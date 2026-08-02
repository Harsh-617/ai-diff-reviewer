import os

from dotenv import load_dotenv

load_dotenv()

AUTH_TOKEN = os.environ["AUTH_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
GROQ_MODEL = os.environ["GROQ_MODEL"]
DATABASE_PATH = os.environ["DATABASE_PATH"]
PORT = int(os.environ.get("PORT", 8000))

MAX_PAYLOAD_BYTES = 1_048_576
CHUNK_BYTES = 65_536
MAX_CONCURRENT_JOBS = 4
RATE_LIMIT_PER_MINUTE = 30
