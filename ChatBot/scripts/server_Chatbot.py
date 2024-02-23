import os
from typing import List, Tuple
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import StreamingResponse, FileResponse
from pydub import AudioSegment
from langserve import add_routes
from langchain_core.pydantic_v1 import BaseModel, Field
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain.prompts import ChatPromptTemplate
from langchain.prompts.prompt import PromptTemplate
from langchain.schema import format_document
from langchain.schema.output_parser import StrOutputParser
from langchain.schema.runnable import RunnableMap, RunnablePassthrough
from langchain.vectorstores.faiss import FAISS
from langchain_community.callbacks import get_openai_callback
from google.cloud import texttospeech_v1 as texttospeech
from google.cloud import speech_v1 as speech
from operator import itemgetter
from werkzeug.utils import secure_filename
from io import BytesIO
AudioSegment.ffmpeg = "C:\\Users\\ferna\\OneDrive\\Escritorio\\ffmpeg\\ffmpeg-6.1.1\\ffmpeg.exe"
#AudioSegment.ffprobe = "C:\\Users\\ferna\\OneDrive\\Escritorio\\ffmpeg\\ffmpeg-6.1.1\\ffprobe.exe"
import librosa
from extract_apis_keys import load
import soundfile as sf
import tempfile
import speech_recognition as sr


app = FastAPI(
    title="LangChain Server",
    version="1.0",
    description="Spin up a simple API server using Langchain's Runnable interfaces",
)

UPLOAD_DIRECTORY = "./audio_files"
os.makedirs(UPLOAD_DIRECTORY, exist_ok=True)

# Configura las credenciales de autenticación de Google Cloud
GOOGLE_APPLICATION_CREDENTIALS = load()

# Carga de la clave de la API OpenAI
OPENAI_API_KEY = load()[1]
openai_embeddings = OpenAIEmbeddings(api_key=OPENAI_API_KEY)

# Plantillas de conversación y respuesta
_TEMPLATE = """Given the following conversation and a follow up question, rephrase the 
follow up question to be a standalone question, in its original language.

Chat History:
{chat_history}
Follow Up Input: {question}
Standalone question:"""
CONDENSE_QUESTION_PROMPT = ChatPromptTemplate.from_template(_TEMPLATE)

ANSWER_TEMPLATE = """Answer the question based only on the following context:
{context}

Question: {question}
"""
ANSWER_PROMPT = ChatPromptTemplate.from_template(ANSWER_TEMPLATE)

DEFAULT_DOCUMENT_PROMPT = ChatPromptTemplate.from_template(template="{page_content}")

# Función para combinar documentos
def _combine_documents(
    docs, document_prompt=DEFAULT_DOCUMENT_PROMPT, document_separator="\n\n"
):
    """Combine documents into a single string."""
    doc_strings = [format_document(doc, document_prompt) for doc in docs]
    return document_separator.join(doc_strings)

# Función para formatear el historial del chat
def _format_chat_history(chat_history: List[Tuple]) -> str:
    """Format chat history into a string."""
    buffer = ""
    for dialogue_turn in chat_history:
        human = "Human: " + dialogue_turn[0]
        ai = "Assistant: " + dialogue_turn[1]
        buffer += "\n" + "\n".join([human, ai])
    return buffer

# Carga del índice de vectores
index_directory = "./faiss_index"
persisted_vectorstore = FAISS.load_local(index_directory, openai_embeddings)
retriever = persisted_vectorstore.as_retriever()

# Definición del mapeo de entrada y contexto
_inputs = RunnableMap(
    standalone_question=RunnablePassthrough.assign(
        chat_history=lambda x: _format_chat_history(x["chat_history"])
    )
    | CONDENSE_QUESTION_PROMPT
    | ChatOpenAI(api_key=OPENAI_API_KEY, temperature=0)
    | StrOutputParser(),
)
_context = {
    "context": itemgetter("standalone_question") | retriever | _combine_documents,
    "question": lambda x: x["standalone_question"],
}

# Definición del modelo de entrada del historial de chat
class ChatHistory(BaseModel):
    """Chat history with the bot."""

    chat_history: List[Tuple[str, str]] = Field(
        ...,
        extra={"widget": {"type": "chat", "input": "question"}},
    )
    question: str

# Cadena de procesamiento de la conversación
conversational_qa_chain = (
    _inputs | _context | ANSWER_PROMPT | ChatOpenAI(model="gpt-4-0125-preview") | StrOutputParser()
)
chain = conversational_qa_chain.with_types(input_type=ChatHistory)

# Variable global para almacenar el historial del chat
global_chat_history = []

# Función para convertir texto a voz (TTS)
def text_to_speech(text):
    client = texttospeech.TextToSpeechClient()
    synthesis_input = texttospeech.SynthesisInput(text=text)
    voice = texttospeech.VoiceSelectionParams(
        language_code="es-ES", ssml_gender=texttospeech.SsmlVoiceGender.FEMALE
    )
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.LINEAR16
    )
    response = client.synthesize_speech(
        input=synthesis_input, voice=voice, audio_config=audio_config
    )
    return response.audio_content

async def speech_to_text(file: UploadFile = File(...)):
    try:
        # Guardar el archivo de audio temporalmente en el disco
        with tempfile.NamedTemporaryFile(delete=False) as temp_audio:
            temp_audio.write(await file.read())
            temp_audio_path = temp_audio.name

        # Realizar la transcripción de voz
        transcription = await speech_to_text_internal(temp_audio_path)

        # Eliminar el archivo temporal
        os.remove(temp_audio_path)

        # Devolver directamente el texto transcribido
        return {"text": transcription}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def speech_to_text_internal(audio_path: str) -> str:
    try:
        # Crear un objeto Recognizer
        recognizer = sr.Recognizer()

        # Abrir el archivo de audio como una fuente de audio
        with sr.AudioFile(audio_path) as source:
            # Escuchar el audio y transcribirlo
            audio_data = recognizer.record(source)
            transcription = recognizer.recognize_google(audio_data, language="es-ES")

        # Devolver la transcripción
        return transcription

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Define la ruta y la función controladora para manejar las solicitudes POST
@app.post("/answer")
async def get_answer(request_body: dict):
    global global_chat_history
    
    # Extraer la pregunta del cuerpo de la solicitud
    question = request_body.get("question")
    if not question:
        raise HTTPException(status_code=400, detail="Pregunta no proporcionada en el cuerpo de la solicitud.")
    
    # Extraer el historial de chat del cuerpo de la solicitud, o usar una lista vacía si no está presente
    chat_history = request_body.get("chat_history", [])
    
    # Llama a tu lógica existente para obtener la respuesta
    with get_openai_callback() as cb:
        respuesta = chain.invoke({"chat_history": chat_history, "question": question})
        print(cb)
        # Si ocurrió algún error al obtener la respuesta, lanza una excepción HTTP
        if not respuesta:
            raise HTTPException(status_code=500, detail="Error al procesar la pregunta")
    
    # Convertir la respuesta del chatbot a audio utilizando la función text_to_speech
    audio_content = text_to_speech(respuesta)
    
    # Actualizar el historial de chat global con la nueva conversación
    global_chat_history.append(("Usuario", question))
    global_chat_history.append(("Asistente", respuesta))
    
    # Crear un archivo temporal para almacenar el contenido de audio
    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp_audio_file:
        tmp_audio_file.write(audio_content)
        tmp_audio_file_path = tmp_audio_file.name

    # Devolver el archivo temporal como respuesta
    return FileResponse(tmp_audio_file_path, media_type="audio/mp3")

@app.post("/speech_to_text")
async def stt(file: UploadFile = File(...)):
    try:
        # Guardar el archivo de audio temporalmente en el disco
        with tempfile.NamedTemporaryFile(delete=False) as temp_audio:
            temp_audio.write(await file.read())
            temp_audio_path = temp_audio.name

        # Realizar la transcripción de voz utilizando la función interna
        transcription = await speech_to_text_internal(temp_audio_path)

        # Eliminar el archivo temporal
        os.remove(temp_audio_path)

        return {"text": transcription}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@app.post("/ask_audio")
async def ask_question_audio(file: UploadFile = File(...)):
    try:
        # Leer el contenido del archivo de audio
        contents = await file.read()

        # Crear un archivo temporal para escribir el contenido
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp_audio_file:
            tmp_audio_file.write(contents)
            tmp_audio_file_path = tmp_audio_file.name

        # Resample a 16000 Hz si la frecuencia de muestreo no coincide
        y, sr = librosa.load(tmp_audio_file_path, sr=None)
        if sr != 16000:
            y_resampled = librosa.resample(y, orig_sr=sr, target_sr=16000)
            sr = 16000
        else:
            y_resampled = y

        # Escribir el audio resampleado en un archivo WAV
        output_path = "temp_audio_resampled.wav"
        sf.write(output_path, y_resampled, sr)

        # Devolver el archivo WAV como respuesta
        return FileResponse(output_path, media_type="audio/wav")

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Ruta para la conversión de texto a voz (TTS)
@app.post("/text_to_speech")
async def tts(text: str):
    audio_content = text_to_speech(text)
    return {"audio_content": audio_content}

# Ruta para ver el historial del chat
@app.get("/chat_history")
async def view_chat_history():
    global global_chat_history
    # La función `view_chat_history` devuelve el historial global del chat
    return {"chat_history": global_chat_history}

# Manejar solicitudes para el ícono de favicon
@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    # Puedes devolver una imagen de ícono si tienes una
    return

# Adds routes to the app for using the chain under:
# /invoke
# /batch
# /stream
add_routes(app, chain, enable_feedback_endpoint=True)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="localhost", port=8000)
