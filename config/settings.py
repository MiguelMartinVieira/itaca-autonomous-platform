"""
Centralized Configuration for Ítaca Autonomous Operations & WhatsApp Automation.
All credentials, endpoints, and identifiers are loaded securely via environment variables.
"""

import os
from pathlib import Path

# ============================================================================
# ENVIRONMENT HELPERS
# ============================================================================

def _get_bool_env(name: str, default: bool = False) -> bool:
    """Reads a boolean environment variable safely."""
    value = os.getenv(name, str(default)).strip().lower()
    return value in ("true", "1", "yes", "on")

# ============================================================================
# DIRECTORY PATHS
# ============================================================================

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
CONFIG_DIR = ROOT_DIR / "config"
LOGS_DIR = ROOT_DIR / "logs"
CREDENTIALS_FILE = CONFIG_DIR / "credentials.json"

# ============================================================================
# RUNTIME & TEST MODE
# ============================================================================

TEST_MODE = _get_bool_env("TEST_MODE", default=True)
TEST_NUMBER = os.getenv("TEST_NUMBER", "+34600000000")
LOG_ONLY_MODE = _get_bool_env("LOG_ONLY_MODE", default=False)
USAR_TELEFONO_PERSONAL = _get_bool_env("USAR_TELEFONO_PERSONAL", default=False)

PAUSA_ENTRE_MENSAJES = float(os.getenv("PAUSA_ENTRE_MENSAJES", "1.0"))
T = {"between": PAUSA_ENTRE_MENSAJES}

ADMIN_PHONE = os.getenv("ADMIN_PHONE", "+34600000000")
INTERCEPTOR_ADMIN_PHONE = ADMIN_PHONE
INTERCEPTOR_CONFIANZA_UMBRAL = float(os.getenv("INTERCEPTOR_CONFIANZA_UMBRAL", "0.50"))
GRUPO_ACADEMIA_ID = os.getenv("GRUPO_ACADEMIA_ID", "120363000000000000@g.us")

# ============================================================================
# AI & LLM MODEL CASCADE (GOOGLE GEMINI)
# ============================================================================

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_API_KEY_PAID = os.getenv("GEMINI_API_KEY_PAID", "")
GEMINI_API_TIMEOUT = int(os.getenv("GEMINI_API_TIMEOUT", "15"))

GEMINI_MODEL_CASCADE = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-1.5-flash",
    "gemini-1.5-flash-8b",
]

# Circuit Breaker thresholds for external LLM API
GEMINI_CIRCUIT_BREAKER_FAILURES = int(os.getenv("GEMINI_CIRCUIT_BREAKER_FAILURES", "3"))
GEMINI_API_CIRCUIT_BREAKER_FAILURES = GEMINI_CIRCUIT_BREAKER_FAILURES
GEMINI_CIRCUIT_BREAKER_TIMEOUT = int(os.getenv("GEMINI_CIRCUIT_BREAKER_TIMEOUT", "60"))
GEMINI_CIRCUIT_BREAKER_BACKOFF = int(os.getenv("GEMINI_CIRCUIT_BREAKER_BACKOFF", "120"))

# ============================================================================
# MICROSERVICES & EXTERNAL ADAPTERS
# ============================================================================

WHATSAPP_SERVICE_PORT = int(os.getenv("WHATSAPP_SERVICE_PORT", "3100"))
WHATSAPP_SERVICE_URL = os.getenv("WHATSAPP_SERVICE_URL", f"http://localhost:{WHATSAPP_SERVICE_PORT}")

# Google Sheets Configuration (Placeholders for public showcase)
SHEET_KEY_BBDD = os.getenv("SHEET_KEY_BBDD", "DEMO_SPREADSHEET_ID_BBDD")
SHEET_KEY_CUENTAS = os.getenv("SHEET_KEY_CUENTAS", "DEMO_SPREADSHEET_ID_CUENTAS")

FILE_CLASES_ESPERADAS = DATA_DIR / "clases_esperadas.csv"
FILE_CLASES_ACTUALES = DATA_DIR / "clases_actuales.csv"

SHEET_GIDS = {
    "Clases Esperadas": int(os.getenv("GID_ESPERADAS", "1001")),
    "Clases Actuales": int(os.getenv("GID_ACTUALES", "1002")),
    "Clases Por Cobrar Itaca": int(os.getenv("GID_COBRAR", "1003")),
    "Clases Por Pagar Padres": int(os.getenv("GID_PAGAR", "1004")),
    "Pagos": int(os.getenv("GID_PAGOS", "1005")),
    "Alumnos": int(os.getenv("GID_ALUMNOS", "0")),
    "Profesores": int(os.getenv("GID_PROFESORES", "1006")),
}

SHEET_COL_CHECK_PADRES = os.getenv("SHEET_COL_CHECK_PADRES", "G")
APPS_SCRIPT_WEBAPP_URL = os.getenv("APPS_SCRIPT_WEBAPP_URL", "https://script.google.com/macros/s/DEMO_APP_KEY/exec")
