import logging
import smtplib
from email.message import EmailMessage

from config.settings import ALERT_EMAILS, SMTP_PASSWORD, SMTP_PORT, SMTP_SERVER, SMTP_USER

logger = logging.getLogger(__name__)


def enviar_alerta_correo(asunto, cuerpo):
    """
    Envía un correo de alerta usando la configuración SMTP.
    Ignora el envío si SMTP_PASSWORD no está configurado.
    """
    if not SMTP_PASSWORD:
        logger.warning("No se puede enviar alerta de correo: SMTP_PASSWORD no está configurado.")
        return False

    msg = EmailMessage()
    msg.set_content(cuerpo)
    msg["Subject"] = asunto
    msg["From"] = SMTP_USER
    msg["To"] = ", ".join(ALERT_EMAILS)

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
        logger.info(f"Alerta de correo enviada: {asunto}")
        return True
    except Exception as e:
        logger.error(f"Error enviando alerta de correo: {e}")
        return False
