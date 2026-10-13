"""
Módulo de envío de mensajes por WhatsApp Web usando el servicio Node.js (API REST).
"""

import logging
from datetime import datetime

import requests as _requests

from config.settings import (
    GRUPO_ACADEMIA_ID,
    LOG_ONLY_MODE,
    LOGS_DIR,
    TEST_MODE,
    TEST_NUMBER,
    WHATSAPP_API_TIMEOUT,
    WHATSAPP_SERVICE_URL,
)
from shared.phone_utils import limpiar_telefono, validar_telefono_e164

logger = logging.getLogger(__name__)


def registrar_error(telefono, motivo, mensaje_preview=""):
    """Registra errores de envío en un archivo de log."""
    log_path = LOGS_DIR / "errores_envio.log"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if len(mensaje_preview) > 50:
        mensaje_preview = mensaje_preview[:47] + "..."

    linea_log = f'{timestamp} | {telefono} | {motivo} | Mensaje: "{mensaje_preview}"\n'

    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(linea_log)
        logger.info(f"   📝 Error registrado en log: {motivo}")
    except Exception as e:
        logger.error(f"   ⚠️ No se pudo escribir en log de errores: {e}")


def _post_send(destino, message):
    """POST /api/send al servicio Node.js. Devuelve (success, error_msg)."""
    try:
        resp = _requests.post(
            f"{WHATSAPP_SERVICE_URL}/api/send",
            json={"phone": destino, "message": message},
            timeout=WHATSAPP_API_TIMEOUT,
        )
        data = resp.json()
        if data.get("success"):
            return True, None
        return False, data.get("error", "Error desconocido")
    except _requests.ConnectionError:
        return False, f"Servicio WhatsApp no disponible ({WHATSAPP_SERVICE_URL})"
    except Exception as e:
        return False, f"Excepción: {e}"


_alerta_correo_enviada = False


def _enviar_via_api(destino, message):
    """Envía mensaje a través del servicio Node.js (whatsapp-web.js)."""
    global _alerta_correo_enviada
    ok, err = _post_send(destino, message)
    if ok:
        logger.info("   ✅ Enviado via API")
        return True
    logger.error(f"   ❌ API: {err}")
    registrar_error(destino, f"API: {err}", message[:50])

    if not _alerta_correo_enviada and (
        "no disponible" in err.lower()
        or "no conectado" in err.lower()
        or "excepción" in err.lower()
    ):
        from shared.email_utils import enviar_alerta_correo

        asunto = "🚨 ERROR CRÍTICO: WhatsApp desconectado en VPS"
        cuerpo = (
            f"El servicio de WhatsApp en el VPS ha fallado al intentar enviar un mensaje.\n\n"
            f"Error reportado: {err}\n"
            f"Destino: {destino}\n\n"
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
        _alerta_correo_enviada = True

    return False


def enviar_mensaje_rapido(phone, message, *args, **kwargs):
    """
    Envía un mensaje por WhatsApp usando el servicio API Node.js.

    Args:
        phone (str): Número de teléfono del destinatario
        message (str): Mensaje a enviar
        *args, **kwargs: Parámetros opcionales ignorados por compatibilidad retrospectiva (ej: es_primero).

    Returns:
        bool: True si el envío fue exitoso
    """
    destino = limpiar_telefono(phone)

    if not destino:
        logger.warning(f"   SALTA: Número vacío o inválido. Original: {phone}")
        registrar_error(str(phone), "Número vacío", message[:50])
        return False

    if not validar_telefono_e164(destino):
        logger.warning(f"   SALTA: Número inválido ({destino}). Original: {phone}")
        registrar_error(destino, "Número inválido", message[:50])
        return False

    logger.info(f"Enviando a {destino}... (modo API)")

    if TEST_MODE:
        dest_original = destino
        destino = TEST_NUMBER
        message = f"[TEST Destino Original: {dest_original}]\n\n{message}"
        logger.info(f"   REDIRIGIENDO A NÚMERO DE TEST: {destino}")

    if LOG_ONLY_MODE:
        logger.info(f"   [LOG-ONLY] Mensaje registrado para {destino}")
        logger.info(f"   [LOG-ONLY] Contenido: {message[:60]}...")
        return True

    return _enviar_via_api(destino, message)


# ─── Mensajes a grupos ────────────────────────────────────────────────────────


def enviar_mensaje_grupo(mensaje, grupo_id=None):
    """
    Envía un mensaje directamente a un grupo de WhatsApp usando su chatId (@g.us).
    """
    if grupo_id is None:
        grupo_id = GRUPO_ACADEMIA_ID

    logger.info("\n📢 Enviando resumen al grupo Academia Ítaca...")

    if LOG_ONLY_MODE:
        logger.info(f"   [LOG-ONLY] Mensaje al grupo: {mensaje[:80]}...")
        return True

    ok, err = _post_send(grupo_id, mensaje)
    if ok:
        logger.info("   ✅ Resumen enviado al grupo")
        return True
    logger.error(f"   ❌ Error enviando al grupo: {err}")
    return False
