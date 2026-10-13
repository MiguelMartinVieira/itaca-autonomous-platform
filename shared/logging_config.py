"""
Configuración centralizada de logs para el proyecto Ítaca.
"""

import logging
import sys
from logging.handlers import RotatingFileHandler

from config.settings import LOGS_DIR


def setup_logging(workflow_name, enable_file_handler=True):
    """
    Configura el sistema de logging para un workflow específico.
    Crea el directorio de logs con rotación automática y ajusta la salida para Windows/UTF-8.

    Args:
        workflow_name: Nombre del workflow (para logs y archivo)
        enable_file_handler: Si True, escribe también a archivo con rotación
    """
    LOGS_DIR.mkdir(exist_ok=True)

    # Formato consistente (estructurado para parsing)
    log_format = "%(asctime)s | %(name)s | %(levelname)s | %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    # Handler de consola
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(log_format, date_format))

    # Handler de archivo con rotación (maxBytes=10MB, backup_count=5)
    file_handler = None
    if enable_file_handler:
        log_file = LOGS_DIR / f"{workflow_name}.log"
        file_handler = RotatingFileHandler(
            log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(log_format, date_format))

    # Configurar el root logger para capturar todos los submódulos (workflows.*, infrastructure.*, shared.*)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # Limpiar handlers previos para evitar duplicados
    if root_logger.handlers:
        root_logger.handlers.clear()

    root_logger.addHandler(console_handler)
    if file_handler:
        root_logger.addHandler(file_handler)

    # Logger principal del workflow
    logger = logging.getLogger(workflow_name)
    logger.setLevel(logging.DEBUG)
    if logger.handlers:
        logger.handlers.clear()

    # Silenciar librerías externas ruidosas
    for noisy_lib in ("urllib3", "google", "googleapiclient", "gspread"):
        logging.getLogger(noisy_lib).setLevel(logging.WARNING)

    # Fix UTF-8 en Windows
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except AttributeError:
            pass

    logger.info(f"--- INICIANDO WORKFLOW: {workflow_name} ---")
    return logger
