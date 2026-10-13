"""
Circuit breaker con backoff exponencial para llamadas a APIs externas.
"""

import logging
from datetime import datetime, timedelta
from enum import Enum
from functools import wraps
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    """Estados del circuit breaker."""

    CLOSED = "closed"  # Funcionando normalmente
    OPEN = "open"  # Fallando, rechazar llamadas
    HALF_OPEN = "half_open"  # Probando si está recuperado


class CircuitBreaker:
    """
    Circuit breaker con backoff exponencial para APIs.

    Funciona así:
    1. CLOSED: Llamadas pasan normalmente. Fallos se cuentan.
    2. Al alcanzar max_failures: → OPEN
    3. OPEN: Las llamadas se rechazan inmediatamente por 'reset_timeout'
    4. Pasado reset_timeout: → HALF_OPEN (prueba una llamada)
    5. Si funciona: → CLOSED (reset de contadores)
    6. Si falla: → OPEN (aumenta reset_timeout con backoff)
    """

    def __init__(
        self,
        name: str,
        max_failures: int = 3,
        reset_timeout: float = 60,
        backoff_multiplier: float = 2.0,
        max_timeout: float = 3600,
    ):
        """
        Args:
            name: Identificador del breaker (para logging)
            max_failures: Fallos consecutivos antes de abrir
            reset_timeout: Segundos antes de probar HALF_OPEN (inicial)
            backoff_multiplier: Multiplicador del timeout en cada fallo
            max_timeout: Timeout máximo (no crecer infinitamente)
        """
        self.name = name
        self.max_failures = max_failures
        self.reset_timeout = reset_timeout
        self.backoff_multiplier = backoff_multiplier
        self.max_timeout = max_timeout

        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time: Optional[datetime] = None
        self.next_attempt_time = datetime.now()

    def call(self, func: Callable, *args, **kwargs) -> Any:
        """
        Ejecuta func() dentro del circuit breaker.

        Returns:
            El resultado de func()

        Raises:
            CircuitBreakerOpenException si el breaker está abierto
        """
        if self.state == CircuitState.OPEN:
            if datetime.now() < self.next_attempt_time:
                raise CircuitBreakerOpenException(
                    f"{self.name} abierto. Próximo intento en "
                    f"{(self.next_attempt_time - datetime.now()).total_seconds():.1f}s"
                )
            self.state = CircuitState.HALF_OPEN
            logger.info(f"{self.name}: Probando recuperación (HALF_OPEN)")

        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
        except Exception:
            self._on_failure()
            raise

    def _on_success(self) -> None:
        """Se ejecuta cuando una llamada funciona."""
        self.success_count += 1
        if self.state == CircuitState.HALF_OPEN:
            logger.info(f"{self.name}: Recuperado. Volviendo a CLOSED")
            self.state = CircuitState.CLOSED
            self.failure_count = 0
            self.success_count = 0

    def _on_failure(self) -> None:
        """Se ejecuta cuando una llamada falla."""
        self.failure_count += 1
        self.last_failure_time = datetime.now()
        logger.warning(f"{self.name}: Fallo {self.failure_count}/{self.max_failures}")

        if self.failure_count >= self.max_failures:
            self._open_circuit()

    def _open_circuit(self) -> None:
        """Abre el circuito con backoff exponencial."""
        timeout = min(
            self.reset_timeout * (self.backoff_multiplier ** (self.failure_count - 1)),
            self.max_timeout,
        )
        self.next_attempt_time = datetime.now() + timedelta(seconds=timeout)
        self.state = CircuitState.OPEN

        logger.error(f"{self.name}: Abierto. Esperando {timeout:.0f}s antes de reintentar")

    def reset(self) -> None:
        """Reset manual del circuito."""
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time = None
        self.next_attempt_time = datetime.now()
        logger.info(f"{self.name}: Reset manual")

    def __repr__(self) -> str:
        return (
            f"CircuitBreaker({self.name}, state={self.state.value}, "
            f"failures={self.failure_count}/{self.max_failures})"
        )


class CircuitBreakerOpenException(Exception):
    """Se lanza cuando el circuit breaker está abierto."""

    pass


# Registro global de circuit breakers
_circuit_breakers: dict[str, CircuitBreaker] = {}


def get_breaker(name: str, **kwargs) -> CircuitBreaker:
    """Obtiene o crea un circuit breaker con el nombre dado."""
    if name not in _circuit_breakers:
        _circuit_breakers[name] = CircuitBreaker(name, **kwargs)
    return _circuit_breakers[name]


def circuit_breaker(
    name: str,
    max_failures: int = 3,
    reset_timeout: float = 60,
    backoff_multiplier: float = 2.0,
):
    """
    Decorador para proteger una función con circuit breaker.

    Uso:
        @circuit_breaker("gemini_api", max_failures=3)
        def call_gemini():
            return requests.post(...)
    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            breaker = get_breaker(
                name,
                max_failures=max_failures,
                reset_timeout=reset_timeout,
                backoff_multiplier=backoff_multiplier,
            )
            return breaker.call(func, *args, **kwargs)

        return wrapper

    return decorator
