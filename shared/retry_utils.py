"""
Utilidades para reintentos con backoff exponencial.
"""

import logging
import time
from functools import wraps
from typing import Any, Callable, Tuple, Type

logger = logging.getLogger(__name__)


class RetryConfig:
    """Configuración para reintentos."""

    def __init__(
        self,
        max_attempts: int = 3,
        initial_delay: float = 1.0,
        backoff_multiplier: float = 2.0,
        max_delay: float = 60.0,
        exceptions: Tuple[Type[Exception], ...] = (Exception,),
    ):
        """
        Args:
            max_attempts: Número máximo de intentos (incluyendo el primero)
            initial_delay: Delay inicial en segundos
            backoff_multiplier: Multiplicador para backoff exponencial
            max_delay: Delay máximo
            exceptions: Excepciones que disparan reintentos
        """
        self.max_attempts = max_attempts
        self.initial_delay = initial_delay
        self.backoff_multiplier = backoff_multiplier
        self.max_delay = max_delay
        self.exceptions = exceptions


def retry(
    max_attempts: int = 3,
    initial_delay: float = 1.0,
    backoff_multiplier: float = 2.0,
    max_delay: float = 60.0,
    exceptions: Tuple[Type[Exception], ...] = (Exception,),
) -> Callable:
    """
    Decorador para reintentar una función con backoff exponencial.

    Uso:
        @retry(max_attempts=3, initial_delay=1.0)
        def call_api():
            return requests.get(...)

    Args:
        max_attempts: Intentos máximos
        initial_delay: Delay inicial en segundos
        backoff_multiplier: Multiplicador de delay
        max_delay: Delay máximo
        exceptions: Tupla de excepciones que disparan reintentos
    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            config = RetryConfig(
                max_attempts=max_attempts,
                initial_delay=initial_delay,
                backoff_multiplier=backoff_multiplier,
                max_delay=max_delay,
                exceptions=exceptions,
            )
            return retry_with_config(func, config, *args, **kwargs)

        return wrapper

    return decorator


def retry_with_config(
    func: Callable,
    config: RetryConfig,
    *args,
    **kwargs,
) -> Any:
    """
    Ejecuta una función con reintentos según la configuración.

    Args:
        func: Función a ejecutar
        config: Configuración de reintentos
        *args, **kwargs: Argumentos para la función

    Returns:
        El resultado de func()

    Raises:
        La última excepción si se agotan los reintentos
    """
    last_exception = None
    delay = min(config.initial_delay, config.max_delay)

    for attempt in range(1, config.max_attempts + 1):
        try:
            return func(*args, **kwargs)
        except config.exceptions as e:
            last_exception = e
            if attempt == config.max_attempts:
                logger.error(
                    f"Fallo final en {func.__name__} después de "
                    f"{config.max_attempts} intentos: {e}"
                )
                raise
            logger.warning(
                f"{func.__name__}: Intento {attempt}/{config.max_attempts} "
                f"falló. Reintentando en {delay:.1f}s..."
            )
            time.sleep(delay)
            delay = min(delay * config.backoff_multiplier, config.max_delay)

    raise last_exception


def retry_on_exception(
    func: Callable,
    max_attempts: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: Tuple[Type[Exception], ...] = (Exception,),
) -> Any:
    """
    Ejecuta una función con reintentos, versión imperativa.

    Uso:
        result = retry_on_exception(
            lambda: requests.get(url),
            max_attempts=3,
            delay=1.0,
            exceptions=(ConnectionError, TimeoutError),
        )
    """
    config = RetryConfig(
        max_attempts=max_attempts,
        initial_delay=delay,
        backoff_multiplier=backoff,
        exceptions=exceptions,
    )
    return retry_with_config(func, config)
