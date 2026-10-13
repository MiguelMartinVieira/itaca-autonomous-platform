"""
Workflow: Watchdog de WhatsApp
Verifica proactivamente el estado del servicio Node.js (whatsapp-web.js).
Envía un correo de alerta si la sesión se desconecta o la API no responde,
evitando spam persistiendo el estado de la alerta.
"""

import logging
from datetime import datetime

import requests

from config.settings import DATA_DIR, TEST_MODE, WHATSAPP_SERVICE_URL
from shared.email_utils import enviar_alerta_correo
from shared.persistent_state import PersistentState

logger = logging.getLogger(__name__)

CSVS_NEEDED = []
STATE_FILE = DATA_DIR / "watchdog_state.json"


def _intentar_autorreiniciar_servicio() -> bool:
    """Intenta reiniciar el servicio systemd en Linux si existe systemctl."""
    import os
    import shutil
    import subprocess
    import time

    # Nunca intentar reiniciar en entornos de pruebas unitarias o desarrollo
    if TEST_MODE or os.getenv("PYTEST_CURRENT_TEST"):
        return False

    if not shutil.which("systemctl"):
        return False

    try:
        logger.info(
            "🔄 Watchdog: Intentando auto-recuperación vía 'systemctl restart itaca-whatsapp'..."
        )
        subprocess.run(
            ["systemctl", "restart", "itaca-whatsapp"],
            check=False,
            timeout=15,
            capture_output=True,
        )
        time.sleep(3)
        return True
    except Exception as e:
        logger.warning(f"⚠️ No se pudo ejecutar el autorreinicio del servicio: {e}")
        return False


def run(auto: bool = True) -> bool:
    """
    Ejecuta el chequeo de salud del servicio WhatsApp.

    Args:
        auto: Modo automático (no requiere interacción).

    Returns:
        bool: True si el servicio está 'connected', False en caso contrario.
    """
    logger.info("🔍 Ejecutando Watchdog de WhatsApp...")

    state = PersistentState(STATE_FILE)
    alert_sent = state.get("alert_sent", False)

    is_connected = False
    error_msg = None

    try:
        resp = requests.get(f"{WHATSAPP_SERVICE_URL}/api/health", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            status = str(data.get("status", "")).lower()
            if status == "connected":
                is_connected = True
            else:
                error_msg = f"Estado reportado por la API: '{status}'"
        else:
            error_msg = f"Respuesta HTTP {resp.status_code} al consultar /api/health"
    except Exception as e:
        error_msg = f"Error de conexión con la API de WhatsApp ({WHATSAPP_SERVICE_URL}): {e}"

    if not is_connected:
        logger.warning(f"⚠️ Watchdog: WhatsApp no disponible en primer chequeo. {error_msg}")
        if _intentar_autorreiniciar_servicio():
            try:
                resp2 = requests.get(f"{WHATSAPP_SERVICE_URL}/api/health", timeout=10)
                if resp2.status_code == 200:
                    status2 = str(resp2.json().get("status", "")).lower()
                    if status2 == "connected":
                        logger.info(
                            "✅ Watchdog: Auto-recuperación exitosa tras reinicio del servicio."
                        )
                        is_connected = True
            except Exception as e2:
                logger.warning(f"Segundo intento de conexión falló tras reinicio: {e2}")

    if is_connected:
        logger.info("✅ Watchdog: Servicio WhatsApp conectado y funcionando correctamente.")
        if alert_sent:
            asunto = "✅ RECUPERADO: WhatsApp reconectado en VPS"
            cuerpo = (
                "El servicio de WhatsApp en el VPS ha vuelto a estar CONECTADO y operativo.\n\n"
                f"Fecha/Hora: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            )
            enviar_alerta_correo(asunto, cuerpo)
            state.set("alert_sent", False)
            state.save()
        return True

    # Si NO está conectado definitivamente
    logger.warning(f"⚠️ Watchdog: WhatsApp no disponible definitivamente. {error_msg}")

    if not alert_sent:
        asunto = "🚨 ALERTA CRÍTICA: WhatsApp desconectado en VPS"
        cuerpo = (
            "Se ha detectado que el servicio de WhatsApp en el VPS NO está conectado.\n\n"
            f"Detalle del error: {error_msg}\n"
            f"Fecha/Hora: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            "Acción requerida para reconectar:\n"
            "1. Conéctate por SSH: ssh vps\n"
            "2. Opción rápida (ver QR en logs):\n"
            "   tail -n 35 /var/log/itaca-whatsapp.log\n"
            "3. Si necesitas reiniciar la sesión en vivo:\n"
            "   systemctl stop itaca-whatsapp\n"
            "   cd /root/Itaca/whatsapp-service && node index.js\n"
            "   (Escanea el QR en pantalla y presiona Ctrl+C tras conectar)\n"
            "   systemctl start itaca-whatsapp\n"
            "4. Comprobar salud:\n"
            "   curl http://localhost:3100/api/health\n"
        )
        enviar_alerta_correo(asunto, cuerpo)
        state.set("alert_sent", True)
        state.set("last_alert_time", datetime.now().isoformat())
        state.set("last_error", error_msg)
        state.save()
    else:
        logger.info(
            "ℹ️ Watchdog: Alerta de correo ya fue enviada previamente. Omitiendo duplicado."
        )

    return False
