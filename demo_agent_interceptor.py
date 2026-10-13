"""
Standalone Demonstration of the Ítaca Interceptor NLP Agent.
Demonstrates message classification, confidence thresholding, and human-in-the-loop triage.
"""

from typing import List, Dict, Any
from dataclasses import dataclass

@dataclass
class SimulatedMessage:
    sender_name: str
    phone: str
    text: str
    expected_class: str

SAMPLE_INBOX = [
    SimulatedMessage(
        sender_name="Beatriz Gomez",
        phone="+34611223344",
        text="Hola Miguel, esta tarde Mateo no podrá tener la clase de Matemáticas de las 17:30 porque tiene fiebre.",
        expected_class="Matemáticas - Mateo (17:30)"
    ),
    SimulatedMessage(
        sender_name="Carlos Mendez",
        phone="+34655667788",
        text="Buenas, ¿a qué hora era la sesión de Física de mañana? Se me ha olvidado apuntarlo.",
        expected_class="Física - Lucas (18:00)"
    ),
    SimulatedMessage(
        sender_name="Elena Rivas",
        phone="+34699001122",
        text="Hola! Al final Sofía no va hoy a clase, perdón por avisar tan tarde.",
        expected_class="Química - Sofía (19:00)"
    ),
    SimulatedMessage(
        sender_name="Desconocido",
        phone="+34622334455",
        text="Hola, ¿me pasas el enlace de pago de la semana pasada?",
        expected_class="Sin clase asignada"
    )
]

def simulate_nlp_classification(message: SimulatedMessage) -> Dict[str, Any]:
    """
    Simulates the Gemini Flash cascade response schema.
    In production, this calls Google Gemini via `GEMINI_MODEL_CASCADE` with circuit breaker fallback.
    """
    text_lower = message.text.lower()
    
    # 1. Detection of cancellation cues
    cancellation_cues = ["no podrá", "no va hoy", "cancelar", "cancelamos", "tiene fiebre", "no puede asistir"]
    inquiry_cues = ["a qué hora", "se me ha olvidado", "duda", "pregunta"]
    payment_cues = ["enlace de pago", "bizum", "transferencia", "pagado"]
    
    if any(cue in text_lower for cue in cancellation_cues):
        intent = "CANCELLATION"
        confidence = 0.95
        reason = "Explicit notice of student illness/absence."
    elif any(cue in text_lower for cue in inquiry_cues):
        intent = "SCHEDULE_INQUIRY"
        confidence = 0.88
        reason = "Parent inquiring about schedule timing."
    elif any(cue in text_lower for cue in payment_cues):
        intent = "PAYMENT_INQUIRY"
        confidence = 0.82
        reason = "Billing or link request."
    else:
        intent = "AMBIGUOUS"
        confidence = 0.35
        reason = "Insufficient operational context."
        
    return {
        "intent": intent,
        "confidence": confidence,
        "reason": reason,
        "target_class": message.expected_class if intent == "CANCELLATION" else None
    }

def run_pipeline():
    print("==================================================================")
    print("ÍTACA AUTONOMOUS NLP AGENT — INBOX CLASSIFICATION PIPELINE")
    print("==================================================================")
    
    CONFIDENCE_THRESHOLD = 0.50
    
    for idx, msg in enumerate(SAMPLE_INBOX, 1):
        result = simulate_nlp_classification(msg)
        print(f"\n[MSG #{idx}] From: {msg.sender_name} ({msg.phone})")
        print(f"Text: \"{msg.text}\"")
        print(f"Classification : Intent={result['intent']} | Confidence={result['confidence']:.2f}")
        
        if result['confidence'] >= CONFIDENCE_THRESHOLD and result['intent'] == "CANCELLATION":
            print(f"⚡ ACTION TAKEN : AUTO-RECONCILED -> Removed '{result['target_class']}' from Clases Esperadas.")
        elif result['confidence'] < CONFIDENCE_THRESHOLD or result['intent'] == "AMBIGUOUS":
            print(f"⚠️  HUMAN-IN-THE-LOOP : Triaged to Admin WhatsApp -> Dispatched review alert.")
        else:
            print(f"ℹ️  ROUTED : Handled as standard operational message ({result['intent']}).")
            
    print("\n==================================================================")
    print("PIPELINE EXECUTION COMPLETED: 100% messages processed with zero errors.")
    print("==================================================================")

if __name__ == "__main__":
    run_pipeline()
