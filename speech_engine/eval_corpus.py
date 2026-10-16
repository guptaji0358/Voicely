"""Fixed evaluation sentences, identical for every backend/profile."""

CORPUS: dict[str, str] = {
    "conversational": "Oh, that's a good question. Let me think about it for a second, and then I'll explain.",
    "question": "Did you really expect it to work the very first time?",
    "exclamation": "That was absolutely incredible! I can't believe we made it.",
    "numbers_dates": "On 3 March 2024, the 14:30 train carried 1,250 passengers at a cost of $7.95 each.",
    "abbreviations": "Dr. Patel and Mr. Jones met at 5 p.m. to discuss e.g. budgets, schedules, etc.",
    "punctuation": "Well — it depends; however, (if you ask me) the answer is: no. Yes? Maybe...",
    "tongue_twister": "She sells seashells by the seashore, and the shells she sells are surely seashells.",
    "difficult_words": "The choir's colonel, a worcestershire cook, pronounced 'quinoa' and 'epitome' awkwardly.",
    "transition": "The results were clear. However, the committee remained unconvinced, so the debate continued late into the night.",
    "ocr_extract": "Chapter 3\n\nThe quick brown fox jumps over the lazy dog. This sen-\ntence was wrapped by an OCR engine at the margin.\n\nA second paragraph follows here.",
    "long_paragraph": (
        "Speech is the most natural interface people have. When a system reads a long document aloud, "
        "small errors compound: a flat sentence ending, a rushed comma, or a slightly different timbre "
        "from one paragraph to the next quickly becomes tiring to hear. A good narrator keeps the same "
        "pace and warmth for pages at a time, lets questions rise, lets statements settle, and leaves "
        "just enough silence between ideas for the listener to follow along."
    ),
    "french": "Bonjour, je m'appelle Claire et j'habite à Lyon depuis trois ans.",
    "spanish": "¿Cómo estás? Hoy hace mucho sol y vamos a la playa.",
    "hindi": "नमस्ते, आज मौसम बहुत अच्छा है और हम बाहर घूमने जा रहे हैं।",
}
# Kokoro v1.0 lang codes used by the local backend for non-English corpus items.
LANG_FOR_ITEM = {"french": "fr-fr", "spanish": "es", "hindi": "hi"}
