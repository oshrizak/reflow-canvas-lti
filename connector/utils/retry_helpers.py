"""Retry logic and error categorization utilities.

Provides standardized retry mechanisms with exponential backoff and
intelligent error categorization to distinguish transient vs permanent failures.
"""

import asyncio
import logging
from collections.abc import Callable
from typing import Any, TypeVar

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

# Type variable for generic return type
T = TypeVar('T')

# Retryable Boto3 error codes
RETRYABLE_BOTO_ERROR_CODES = [
    'RequestTimeout',
    'ServiceUnavailable',
    'ThrottlingException',
    'RequestThrottled',
    'InternalError',
    'SlowDown',
    'TooManyRequestsException',
    'ProvisionedThroughputExceededException'
]

# Retryable HTTP status codes
RETRYABLE_HTTP_CODES = [408, 429, 500, 502, 503, 504]

# Non-retryable Boto3 error codes (permanent failures)
NON_RETRYABLE_BOTO_ERROR_CODES = [
    'NoSuchKey',
    'NoSuchBucket',
    'AccessDenied',
    'InvalidRequest',
    'InvalidArgument',
    'MalformedXML',
    'InvalidBucketName'
]


def is_retryable_error(error: Exception) -> bool:
    """Check if an error should trigger a retry.

    Categorizes errors into:
    - Retryable: Transient network errors, timeouts, throttling, service unavailable
    - Non-retryable: Invalid requests, access denied, resource not found

    Args:
        error: Exception to categorize

    Returns:
        bool: True if error is retryable, False if permanent failure

    Examples:
        >>> is_retryable_error(ClientError({'Error': {'Code': 'RequestTimeout'}}, 'op'))
        True
        >>> is_retryable_error(ClientError({'Error': {'Code': 'NoSuchKey'}}, 'op'))
        False
        >>> is_retryable_error(asyncio.TimeoutError())
        True
    """
    # Boto3 ClientError (structured AWS errors)
    if isinstance(error, ClientError):
        error_code = error.response.get('Error', {}).get('Code', '')
        http_code = error.response.get('ResponseMetadata', {}).get('HTTPStatusCode')

        # Check if explicitly non-retryable
        if error_code in NON_RETRYABLE_BOTO_ERROR_CODES:
            logger.debug(f"Non-retryable Boto error: {error_code}")
            return False

        # Check if explicitly retryable
        if error_code in RETRYABLE_BOTO_ERROR_CODES:
            logger.debug(f"Retryable Boto error: {error_code}")
            return True

        # Check HTTP status code
        if http_code in RETRYABLE_HTTP_CODES:
            logger.debug(f"Retryable HTTP code: {http_code}")
            return True

        # Default: treat unknown ClientErrors as non-retryable
        return False

    # Boto3 low-level errors (network issues)
    if isinstance(error, BotoCoreError):
        logger.debug(f"Retryable BotoCoreError: {type(error).__name__}")
        return True

    # Network/timeout errors
    if isinstance(error, (asyncio.TimeoutError, ConnectionError, TimeoutError)):
        logger.debug(f"Retryable network error: {type(error).__name__}")
        return True

    # Redis errors (connection issues)
    if isinstance(error, (RedisError, RedisConnectionError)):
        logger.debug(f"Retryable Redis error: {type(error).__name__}")
        return True

    # HTTPException from storage/queue services (check status code)
    if isinstance(error, HTTPException):
        # 5xx errors are retryable (server errors)
        # 408 (Request Timeout) and 429 (Too Many Requests) are retryable
        if error.status_code in [408, 429, 500, 502, 503, 504]:
            logger.debug(f"Retryable HTTPException: {error.status_code}")
            return True
        # 4xx errors (except timeout/throttle) are non-retryable
        logger.debug(f"Non-retryable HTTPException: {error.status_code}")
        return False

    # Default: non-retryable
    logger.debug(f"Non-retryable error: {type(error).__name__}")
    return False


async def retry_with_backoff(
    func: Callable[[], Any],
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    backoff_factor: float = 2.0,
    operation_name: str = "operation"
) -> T:
    """Execute async function with exponential backoff retry logic.

    Retries only on transient errors identified by is_retryable_error().
    Implements exponential backoff with jitter to avoid thundering herd.

    Args:
        func: Async function to execute (takes no args, use lambda if needed)
        max_attempts: Maximum number of attempts (default: 3)
        base_delay: Initial delay in seconds (default: 1.0)
        max_delay: Maximum delay in seconds (default: 30.0)
        backoff_factor: Multiplier for exponential backoff (default: 2.0)
        operation_name: Name of operation for logging (default: "operation")

    Returns:
        Result from successful function execution

    Raises:
        Exception: Re-raises the last exception if all retries fail or if non-retryable

    Examples:
        >>> async def download():
        ...     return await s3_client.get_object(Bucket='b', Key='k')
        >>> result = await retry_with_backoff(download, max_attempts=3, operation_name="S3 download")

        >>> # With lambda for functions with arguments
        >>> result = await retry_with_backoff(
        ...     lambda: storage.download_temp_file(key),
        ...     max_attempts=3,
        ...     operation_name=f"download {key}"
        ... )
    """
    last_exception = None

    for attempt in range(1, max_attempts + 1):
        try:
            logger.debug(f"{operation_name}: attempt {attempt}/{max_attempts}")
            result: T = await func()

            if attempt > 1:
                logger.info(
                    f"{operation_name}: succeeded on attempt {attempt}/{max_attempts}"
                )

            return result

        except Exception as e:
            last_exception = e

            # Check if error is retryable
            if not is_retryable_error(e):
                logger.warning(
                    f"{operation_name}: non-retryable error on attempt {attempt}: {e}"
                )
                raise

            # Last attempt - don't retry
            if attempt == max_attempts:
                logger.error(
                    f"{operation_name}: failed after {max_attempts} attempts: {e}"
                )
                raise

            # Calculate delay with exponential backoff
            delay = min(base_delay * (backoff_factor ** (attempt - 1)), max_delay)

            logger.warning(
                f"{operation_name}: retryable error on attempt {attempt}/{max_attempts}: "
                f"{type(e).__name__}: {e}. Retrying in {delay:.1f}s..."
            )

            await asyncio.sleep(delay)

    # Should never reach here, but for type safety
    if last_exception:
        raise last_exception
    raise RuntimeError(f"{operation_name}: unexpected retry loop exit")


async def retry_with_backoff_for_sync_func(
    func: Callable[[], Any],
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    backoff_factor: float = 2.0,
    operation_name: str = "operation"
) -> T:
    """Async wrapper for retrying synchronous (blocking) functions with exponential backoff.

    This is an async function that wraps synchronous (blocking) functions and provides
    retry logic with exponential backoff. Uses asyncio.sleep for delays to work in async context.

    Args:
        func: Synchronous function to execute
        max_attempts: Maximum number of attempts (default: 3)
        base_delay: Initial delay in seconds (default: 1.0)
        max_delay: Maximum delay in seconds (default: 30.0)
        backoff_factor: Multiplier for exponential backoff (default: 2.0)
        operation_name: Name of operation for logging (default: "operation")

    Returns:
        Result from successful function execution

    Raises:
        Exception: Re-raises the last exception if all retries fail or if non-retryable
    """
    last_exception = None

    for attempt in range(1, max_attempts + 1):
        try:
            logger.debug(f"{operation_name}: attempt {attempt}/{max_attempts}")
            result: T = func()

            if attempt > 1:
                logger.info(
                    f"{operation_name}: succeeded on attempt {attempt}/{max_attempts}"
                )

            return result

        except Exception as e:
            last_exception = e

            # Check if error is retryable
            if not is_retryable_error(e):
                logger.warning(
                    f"{operation_name}: non-retryable error on attempt {attempt}: {e}"
                )
                raise

            # Last attempt - don't retry
            if attempt == max_attempts:
                logger.error(
                    f"{operation_name}: failed after {max_attempts} attempts: {e}"
                )
                raise

            # Calculate delay with exponential backoff
            delay = min(base_delay * (backoff_factor ** (attempt - 1)), max_delay)

            logger.warning(
                f"{operation_name}: retryable error on attempt {attempt}/{max_attempts}: "
                f"{type(e).__name__}: {e}. Retrying in {delay:.1f}s..."
            )

            await asyncio.sleep(delay)

    # Should never reach here, but for type safety
    if last_exception:
        raise last_exception
    raise RuntimeError(f"{operation_name}: unexpected retry loop exit")
