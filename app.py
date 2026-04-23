from flask import Flask, request, render_template, send_file, jsonify, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import whisper, math, os, uuid, threading, zipfile, requests, subprocess, json, re, shutil, sqlite3, stripe
from pydub import AudioSegment
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from PIL import Image, ExifTags

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "veo3-secret-key-mude-isso")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///veo3.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"

# ── Stripe Config ────────────────────────────────────────
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
stripe.api_key = STRIPE_SECRET_KEY

CREDITOS_POR_IMAGEM = 25

PLANOS_STRIPE = {
    "api_propria": {
        "nome": "API Própria",
        "price_id": "price_1TMz6zLW3ZSF3MIl9ZDwFH5y",
        "creditos": 0,
        "valor": "R$29,90/mês",
        "tipo": "assinatura",
        "descricao": "Use suas próprias chaves de API - geração ilimitada"
    },
    "basico": {
        "nome": "Básico",
        "price_id": "price_1TMz70LW3ZSF3MIlS9fHRJL6",
        "creditos": 500,
        "valor": "R$39,90/mês",
        "tipo": "assinatura",
        "descricao": "500 créditos por mês"
    },
    "pro": {
        "nome": "Pro",
        "price_id": "price_1TMz70LW3ZSF3MIlIVzrz2lH",
        "creditos": 1500,
        "valor": "R$79,90/mês",
        "tipo": "assinatura",
        "descricao": "1.500 créditos por mês"
    },
    "business": {
        "nome": "Business",
        "price_id": "price_1TMz71LW3ZSF3MIl4l8AD42S",
        "creditos": 4000,
        "valor": "R$149,90/mês",
        "tipo": "assinatura",
        "descricao": "4.000 créditos por mês"
    },
}

PACOTES_AVULSO = {
    "mini": {
        "nome": "Mini - 200 créditos",
        "price_id": "price_1TMz71LW3ZSF3MIlbXO35Z5Z",
        "creditos": 200,
        "valor": "R$9,90",
        "descricao": "200 créditos extras"
    },
    "grande": {
        "nome": "Grande - 1.500 créditos",
        "price_id": "price_1TMz72LW3ZSF3MIlPc1o5Wx6",
        "creditos": 1500,
        "valor": "R$49,90",
        "descricao": "1.500 créditos extras"
    },
    "ultra": {
        "nome": "Ultra - 7.000 créditos",
        "price_id": "price_1TMz73LW3ZSF3MIliWd0gqYW",
        "creditos": 7000,
        "valor": "R$199,90",
        "descricao": "7.000 créditos extras"
    },
}

# Mapa reverso price_id -> info do plano
PRICE_MAP = {}
for key, p in PLANOS_STRIPE.items():
    PRICE_MAP[p["price_id"]] = {**p, "key": key, "tipo": "assinatura"}
for key, p in PACOTES_AVULSO.items():
    PRICE_MAP[p["price_id"]] = {**p, "key": key, "tipo": "avulso"}

UPLOAD_FOLDER = "uploads"
OUTPUT_FOLDER = "outputs"
STORYBOARD_FOLDER = "storyboards"
BANCO_IMG_FOLDER = "banco_imagens"
PROMPTS_FILE = "prompts_config.json"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)
os.makedirs(STORYBOARD_FOLDER, exist_ok=True)
os.makedirs(BANCO_IMG_FOLDER, exist_ok=True)

jobs = {}
whisper_model = None

# Prompts padrão - editáveis pelo admin
DEFAULT_PROMPTS = {
    "melhorar": """You are a technical prompt engineer for DALL-E 3.
Goal: Convert narration text into a HIGH-QUALITY VERTICAL image prompt.

STRICT RULES:
1. STYLE FIRST: Start the prompt with: "{estilo}" of...". This is mandatory.
2. VERTICAL COMPOSITION: Use "vertical composition", "tall frame", or "full-body portrait" to force 9:16 framing.
3. SCENE ACCURACY: Describe EXACTLY what the text says. Include ALL characters, objects, and actions mentioned.
4. HISTORICAL ACCURACY: For biblical/ancient themes use ancient Middle Eastern attire, rough linen textures, desert sun, period-accurate sandals.
5. SAFE DESCRIPTIONS: Instead of "defeated" say "lying on the ground exhausted". A giant = "extremely tall muscular man". A king = "man wearing golden crown and royal robes".
6. ABSOLUTELY NO TEXT IN IMAGE: Add "no text, no letters, no words, no writing, no captions, no watermarks, no signatures, no inscriptions" at the END of every prompt. This is MANDATORY.
7. NEGATIVE CONCEPTS: Instead of "empty bowl", say "a clean bowl with nothing inside it, completely bare". Instead of "dark room" say "a room with very dim lighting". Always describe what IS there, not what ISN'T.
8. Output ONLY the prompt. Max 400 characters.""",
    "suavizar": "Rewrite this image prompt to pass DALL-E safety filters. Remove ALL violence, weapons, blood, fighting, death, killing. Keep same characters, historical period, and setting. Replace conflict with dramatic tension shown through facial expressions, body language, and atmosphere. Characters must wear period-appropriate clothing. You MUST preserve the original artistic style tags and the vertical composition instruction at all costs. Output ONLY the new prompt.",
    "dividir": """You are a storyboard director. Split this narration into individual scenes.

RULES:
1. Split by logical story beats (not just punctuation).
2. Keep the ORIGINAL text for each scene - do NOT add details or rewrite.
3. Each scene = one short sentence from the original text.
4. Output each scene on a NEW LINE. Nothing else.
5. Keep the SAME LANGUAGE as the input.
6. Do NOT add descriptions, visual details, or embellishments."""
}

def load_prompts():
    try:
        if os.path.exists(PROMPTS_FILE):
            with open(PROMPTS_FILE) as f:
                return json.load(f)
    except: pass
    return DEFAULT_PROMPTS.copy()

def save_prompts(prompts):
    with open(PROMPTS_FILE, "w") as f:
        json.dump(prompts, f, indent=2)

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(150), unique=True, nullable=False)
    nome = db.Column(db.String(100), nullable=False)
    senha = db.Column(db.String(200), nullable=False)
    provider = db.Column(db.String(20), default="")
    api_key = db.Column(db.String(200), default="")
    image_size = db.Column(db.String(20), default="1024x1024")
    quality = db.Column(db.String(20), default="standard")
    minimax_key = db.Column(db.String(200), default="")
    minimax_group_id = db.Column(db.String(100), default="")
    vozes_clonadas = db.Column(db.Text, default="[]")
    is_admin = db.Column(db.Boolean, default=False)
    creditos = db.Column(db.Integer, default=0)
    plano = db.Column(db.Text, default="")
    stripe_customer_id = db.Column(db.Text, default="")
    criado_em = db.Column(db.DateTime, default=datetime.utcnow)
    criacoes = db.relationship("Criacao", backref="user", lazy=True)

    def get_vozes_clonadas(self):
        try: return json.loads(self.vozes_clonadas or "[]")
        except: return []

    def add_voz_clonada(self, nome, voice_id):
        vozes = self.get_vozes_clonadas()
        vozes = [v for v in vozes if v["voice_id"] != voice_id]
        vozes.append({"nome": nome, "voice_id": voice_id})
        self.vozes_clonadas = json.dumps(vozes)

    def tem_creditos(self, qtd=None):
        if qtd is None:
            qtd = CREDITOS_POR_IMAGEM
        # Plano API Própria = ilimitado (usa chave do user)
        if self.plano == "api_propria":
            return True
        # Admin = ilimitado
        if self.is_admin:
            return True
        return self.creditos >= qtd

    def gastar_creditos(self, qtd=None):
        if qtd is None:
            qtd = CREDITOS_POR_IMAGEM
        if self.plano == "api_propria" or self.is_admin:
            return True
        if self.creditos >= qtd:
            self.creditos -= qtd
            return True
        return False

    def get_plano_info(self):
        if self.plano and self.plano in PLANOS_STRIPE:
            return PLANOS_STRIPE[self.plano]
        return None


class Criacao(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    job_id = db.Column(db.String(50), nullable=False)
    nome = db.Column(db.String(200), default="")
    total_imagens = db.Column(db.Integer, default=0)
    zip_path = db.Column(db.String(300), default="")
    video_path = db.Column(db.String(300), default="")
    criado_em = db.Column(db.DateTime, default=datetime.utcnow)


class BancoImagens(db.Model):
    __tablename__ = "banco_imagens"
    id = db.Column(db.Integer, primary_key=True)
    prompt = db.Column(db.Text)
    estilo = db.Column(db.Text)
    tags = db.Column(db.Text)
    path = db.Column(db.Text)
    criado_em = db.Column(db.DateTime, default=datetime.utcnow)


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

def get_whisper_model():
    global whisper_model
    if whisper_model is None:
        whisper_model = whisper.load_model("small")
    return whisper_model

VOZES_MINIMAX = [
    {"id": "Wise_Woman", "nome": "Mulher Sabia"},
    {"id": "Friendly_Person", "nome": "Pessoa Amigavel"},
    {"id": "Inspirational_girl", "nome": "Garota Inspiradora"},
    {"id": "Deep_Voice_Man", "nome": "Voz Grave Masculina"},
    {"id": "Calm_Woman", "nome": "Mulher Calma"},
    {"id": "Casual_Guy", "nome": "Homem Casual"},
    {"id": "Lively_Girl", "nome": "Garota Animada"},
    {"id": "Patient_Man", "nome": "Homem Paciente"},
    {"id": "Young_Knight", "nome": "Jovem Cavaleiro"},
    {"id": "Determined_Man", "nome": "Homem Determinado"},
    {"id": "Lovely_Girl", "nome": "Garota Encantadora"},
    {"id": "Decent_Boy", "nome": "Rapaz Educado"},
    {"id": "Imposing_Manner", "nome": "Voz Imponente"},
    {"id": "Elegant_Man", "nome": "Homem Elegante"},
    {"id": "Sweet_Girl_2", "nome": "Garota Doce"},
    {"id": "Exuberant_Girl", "nome": "Garota Exuberante"},
]

ESTILOS_DETALHADOS = {
    "cinematic, dramatic lighting, photorealistic, 8k": "A professional cinematic film still, shot on 35mm lens, shallow depth of field, dramatic rim lighting, cinematic color grading, highly detailed textures, 8K, vertical composition",
    "cartoon style, vibrant colors, flat design, illustration": "A vibrant cartoon illustration, clean vector style, bold outlines, expressive character design, bright flat colors, vertical orientation",
    "anime style, detailed, colorful, japanese animation": "A detailed anime art scene, cel shading, vibrant colors, expressive eyes, dynamic pose, Japanese animation style, vertical composition",
    "watercolor painting, soft colors, artistic, hand painted": "A traditional watercolor painting, soft pastel colors, visible brush strokes, dreamy atmosphere, artistic composition, vertical canvas",
    "3D render, octane render, highly detailed, studio lighting": "A high-end stylized 3D digital render, Octane render, unreal engine 5 style, volumetric lighting, raytracing, vertical framing",
    "oil painting, classical art, renaissance style": "A classical oil painting, rich warm colors, dramatic chiaroscuro, visible canvas texture, Renaissance aesthetic, vertical masterpiece",
    "minimalist, clean lines, simple shapes, modern design": "A minimalist design composition, clean geometric shapes, limited color palette, modern aesthetic, lots of negative space, vertical layout",
    "dark fantasy, dramatic, moody atmosphere, epic": "A dark fantasy art scene, moody dramatic atmosphere, epic scale, dark palette with accent lighting, concept art quality, vertical composition",
    "vintage photography, film grain, retro colors, 1970s": "A grainy authentic 1970s photograph, Kodak film grain, warm retro color tones, slightly faded, period-accurate details, vertical frame",
    "neon colors, cyberpunk, futuristic city, glowing lights": "A cyberpunk scene, neon glow lighting, futuristic setting, rain-slicked streets, holographic elements, vertical composition",
}

def limpar_texto(texto):
    texto = re.sub(r'[^\w\s\.,!?;:\-\'"()]', ' ', texto, flags=re.UNICODE)
    return re.sub(r'\s+', ' ', texto).strip()

def corrigir_orientacao(img_path):
    try:
        img = Image.open(img_path)
        try:
            for orientation in ExifTags.TAGS.keys():
                if ExifTags.TAGS[orientation] == 'Orientation':
                    break
            exif = img._getexif()
            if exif and orientation in exif:
                if exif[orientation] == 3: img = img.rotate(180, expand=True)
                elif exif[orientation] == 6: img = img.rotate(270, expand=True)
                elif exif[orientation] == 8: img = img.rotate(90, expand=True)
        except: pass
        w, h = img.size
        if w > h * 1.2:
            img = img.rotate(90, expand=True)
        img.save(img_path, quality=95)
        img.close()
    except: pass

def salvar_no_banco(prompt, estilo, img_path, tipo="imagem", categoria=""):
    """Salva imagem ou vídeo no banco com metadados completos"""
    try:
        ext = img_path.rsplit(".", 1)[-1].lower() if "." in img_path else "png"
        nome = f"{uuid.uuid4().hex[:12]}.{ext}"
        destino = os.path.join(BANCO_IMG_FOLDER, nome)
        shutil.copy(img_path, destino)
        conn = sqlite3.connect('instance/veo3.db')
        # Criar colunas novas se não existirem
        try:
            conn.execute("ALTER TABLE banco_imagens ADD COLUMN tipo TEXT DEFAULT 'imagem'")
        except: pass
        try:
            conn.execute("ALTER TABLE banco_imagens ADD COLUMN categoria TEXT DEFAULT ''")
        except: pass
        try:
            conn.execute("ALTER TABLE banco_imagens ADD COLUMN descricao TEXT DEFAULT ''")
        except: pass
        conn.execute("INSERT INTO banco_imagens (prompt, estilo, tags, path, tipo, categoria, descricao) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (prompt, estilo, prompt.lower(), destino, tipo, categoria, prompt))
        conn.commit()
        conn.close()
    except Exception as e:
        import sys
        sys.stderr.write(f"[BANCO] Erro ao salvar: {e}\n")
        sys.stderr.flush()

# Rastrear imagens já usadas no job atual pra não repetir
_imagens_usadas = set()

def resetar_banco_usadas():
    global _imagens_usadas
    _imagens_usadas = set()

def buscar_no_banco(texto, estilo):
    global _imagens_usadas
    try:
        conn = sqlite3.connect('instance/veo3.db')
        palavras = [p for p in texto.lower().split() if len(p) >= 4]
        if not palavras:
            conn.close()
            return None

        # Buscar imagens que contenham o MÁXIMO de palavras da cena
        melhor_match = None
        melhor_score = 0
        rows = conn.execute("SELECT id, path, tags FROM banco_imagens WHERE estilo = ? ORDER BY id DESC LIMIT 100",
                            (estilo,)).fetchall()
        for row in rows:
            if row[0] in _imagens_usadas or not os.path.exists(row[1]):
                continue
            tags = row[2].lower() if row[2] else ""
            # Contar quantas palavras da cena aparecem nas tags
            score = sum(1 for p in palavras if p in tags)
            if score > melhor_score:
                melhor_score = score
                melhor_match = row

        # Só usa se pelo menos 40% das palavras relevantes bateram
        if melhor_match and melhor_score >= max(2, len(palavras) * 0.4):
            _imagens_usadas.add(melhor_match[0])
            conn.close()
            return melhor_match[1]

        conn.close()
    except: pass
    return None

def suavizar_prompt(prompt, api_key):
    prompts = load_prompts()
    try:
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        body = {"model": "gpt-4o-mini", "messages": [
            {"role": "system", "content": prompts.get("suavizar", DEFAULT_PROMPTS["suavizar"])},
            {"role": "user", "content": prompt}
        ], "max_tokens": 300}
        r = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=body, timeout=30)
        if r.ok:
            return r.json()["choices"][0]["message"]["content"].strip()
    except: pass
    return "a dramatic historical scene with warm golden light, ancient setting, photorealistic, 8K, vertical composition"

def melhorar_prompt(texto, estilo, api_key, contexto_roteiro="", ficha_personagens=""):
    prompts = load_prompts()
    estilo_det = ESTILOS_DETALHADOS.get(estilo, estilo) if estilo else "photorealistic, natural lighting, high quality, vertical composition"
    try:
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        system = prompts.get("melhorar", DEFAULT_PROMPTS["melhorar"]).replace("{estilo}", estilo_det)
        if ficha_personagens:
            system += f"""

CHARACTER SHEET (use these EXACT descriptions in every image):
{ficha_personagens}

Full story: "{contexto_roteiro}"
Current scene to illustrate: "{texto}"

RULES:
1. Illustrate EXACTLY what the current scene describes. Include ALL elements mentioned: characters, objects, actions, setting.
2. Use the character descriptions from the sheet above to keep them visually consistent.
3. If the scene mentions a "dono/owner", show that person. If it mentions a "pote/bowl", show that object.
4. EVERY noun in the scene description must appear in the image.
5. NEVER omit characters or objects that are mentioned in the current scene.
6. The scene description is the PRIORITY - show everything it says."""
        elif contexto_roteiro:
            system += f"""

CRITICAL - CHARACTER CONSISTENCY:
Full story: "{contexto_roteiro}"
Current scene: "{texto}"
Keep ALL characters visually identical across scenes. NEVER change species, color, or appearance."""

        body = {"model": "gpt-4o-mini", "messages": [
            {"role": "system", "content": system}, {"role": "user", "content": texto}
        ], "max_tokens": 300}
        r = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=body, timeout=30)
        if r.ok:
            return r.json()["choices"][0]["message"]["content"].strip()
    except: pass
    return f"{estilo_det} of {texto}, no text no words"

def extrair_personagens(roteiro, api_key):
    """Extrai ficha de personagens e elementos-chave do roteiro pra manter consistência visual"""
    try:
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        system = """You are a character and scene designer for an image generation pipeline.
Given a story, extract ALL characters AND important recurring objects.

OUTPUT FORMAT (one per line):
NAME: detailed physical description

RULES:
1. For animals: species, breed, color, size, eye color, distinctive markings
2. For humans: gender, approximate age, skin tone, hair color/style, clothing, build
3. For important objects: color, size, material, distinctive features
4. Be EXTREMELY specific - these descriptions will be copy-pasted into image prompts
5. Output ONLY the descriptions, nothing else
6. Write descriptions in ENGLISH even if the story is in another language"""
        body = {"model": "gpt-4o-mini", "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": roteiro}
        ], "max_tokens": 400}
        r = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=body, timeout=30)
        if r.ok:
            return r.json()["choices"][0]["message"]["content"].strip()
    except: pass
    return ""

def gerar_audio_minimax(texto, api_key, group_id, voice_id, output_path):
    url = f"https://api.minimaxi.chat/v1/t2a_v2?GroupId={group_id}"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {"model": "speech-02-hd", "text": texto, "stream": False,
            "voice_setting": {"voice_id": voice_id, "speed": 1.0, "vol": 1.0, "pitch": 0},
            "audio_setting": {"sample_rate": 32000, "bitrate": 128000, "format": "mp3"}}
    r = requests.post(url, headers=headers, json=body, timeout=60)
    if not r.ok:
        raise Exception(f"MiniMax TTS erro {r.status_code}: {r.text}")
    data = r.json()
    if data.get("base_resp", {}).get("status_code") != 0:
        raise Exception(f"MiniMax erro: {data.get('base_resp')}")
    with open(output_path, "wb") as f:
        f.write(bytes.fromhex(data["data"]["audio"]))

def clonar_voz_minimax(api_key, group_id, audio_path, voice_id):
    url_upload = f"https://api.minimaxi.chat/v1/files/upload?GroupId={group_id}"
    headers = {"Authorization": f"Bearer {api_key}"}
    with open(audio_path, "rb") as f:
        r = requests.post(url_upload, headers=headers,
                          files={"file": (os.path.basename(audio_path), f, "audio/mpeg")},
                          data={"purpose": "voice_clone"}, timeout=60)
    if not r.ok:
        raise Exception(f"MiniMax upload erro {r.status_code}: {r.text}")
    file_id = r.json().get("file", {}).get("file_id")
    if not file_id:
        raise Exception(f"MiniMax upload sem file_id: {r.json()}")
    url_clone = f"https://api.minimaxi.chat/v1/voice_clone?GroupId={group_id}"
    r2 = requests.post(url_clone, headers={**headers, "Content-Type": "application/json"},
                       json={"file_id": int(file_id), "voice_id": voice_id, "need_noise_reduction": True, "need_volume_normalization": True}, timeout=120)
    if not r2.ok:
        raise Exception(f"MiniMax clone erro {r2.status_code}: {r2.text}")
    result = r2.json()
    if result.get("base_resp", {}).get("status_code") != 0:
        raise Exception(f"MiniMax clone falhou: {result.get('base_resp')}")
    return voice_id

def remover_silencio(audio_path, output_path):
    cmd = ["ffmpeg", "-y", "-i", audio_path,
           "-af", "silenceremove=stop_periods=-1:stop_duration=0.3:stop_threshold=-40dB",
           "-c:a", "libmp3lame", "-q:a", "2", output_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not os.path.exists(output_path):
        shutil.copy(audio_path, output_path)

# ── MiniMax Video (Image-to-Video) ──────────────────────
def gerar_video_minimax(img_path, prompt, api_key, output_path, duracao=6):
    """Gera um clipe de vídeo animado a partir de uma imagem usando MiniMax Hailuo"""
    import base64, time
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    # Converter imagem pra base64
    with open(img_path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode()
    ext = img_path.rsplit(".", 1)[-1].lower()
    if ext == "png":
        mime = "image/png"
    else:
        mime = "image/jpeg"
    img_data_url = f"data:{mime};base64,{img_b64}"

    # Criar task
    body = {
        "model": "I2V-01",
        "prompt": prompt,
        "first_frame_image": img_data_url,
    }
    r = requests.post("https://api.minimax.io/v1/video_generation", headers=headers, json=body, timeout=60)
    if not r.ok:
        raise Exception(f"MiniMax Video erro {r.status_code}: {r.text}")
    data = r.json()
    task_id = data.get("task_id")
    if not task_id:
        raise Exception(f"MiniMax Video sem task_id: {data}")

    # Poll status
    for _ in range(120):  # max 20 min
        time.sleep(10)
        r2 = requests.get("https://api.minimax.io/v1/query/video_generation",
                          headers=headers, params={"task_id": task_id}, timeout=30)
        if not r2.ok:
            continue
        status_data = r2.json()
        status = status_data.get("status", "")
        if status == "Success":
            file_id = status_data.get("file_id")
            if not file_id:
                raise Exception("MiniMax Video: sucesso mas sem file_id")
            # Download
            r3 = requests.get("https://api.minimax.io/v1/files/retrieve",
                              headers=headers, params={"file_id": file_id}, timeout=30)
            if not r3.ok:
                raise Exception(f"MiniMax Video download erro: {r3.text}")
            download_url = r3.json().get("file", {}).get("download_url")
            if not download_url:
                raise Exception("MiniMax Video: sem download_url")
            video_r = requests.get(download_url, timeout=120)
            with open(output_path, "wb") as f:
                f.write(video_r.content)
            return True
        elif status == "Fail":
            raise Exception(f"MiniMax Video falhou: {status_data.get('error_message', 'erro desconhecido')}")
    raise Exception("MiniMax Video: timeout aguardando geração")

def gerar_imagem_openai(prompt, api_key, size, quality, output_path, modelo="dall-e-3"):
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    if modelo == "gpt-image-1":
        import base64
        # gpt-image-1 aceita: 1024x1024, 1024x1536, 1536x1024, auto
        size_map = {"1024x1024": "1024x1024", "1792x1024": "1536x1024", "1024x1792": "1024x1536"}
        gpt_size = size_map.get(size, "1024x1536")
        body = {"model": "gpt-image-1", "prompt": prompt, "n": 1, "size": gpt_size, "quality": "high", "output_format": "png"}
        r = requests.post("https://api.openai.com/v1/images/generations", headers=headers, json=body, timeout=120)
        if r.ok:
            data = r.json()
            img_bytes = base64.b64decode(data["data"][0]["b64_json"])
            with open(output_path, "wb") as f:
                f.write(img_bytes)
            return
        erro = r.json().get("error", {}).get("message", "")
        if "model" in erro.lower() or "access" in erro.lower() or "permission" in erro.lower():
            print(f"[IMG] gpt-image-1 indisponivel, usando dall-e-3: {erro}")
            modelo = "dall-e-3"
        else:
            raise Exception(f"OpenAI erro {r.status_code}: {erro}")

    # DALL-E 3
    tamanhos_validos = ["1024x1024", "1792x1024", "1024x1792"]
    if size not in tamanhos_validos:
        size = "1024x1024"
    for tentativa in range(3):
        body = {"model": "dall-e-3", "prompt": prompt, "n": 1, "size": size, "quality": quality, "response_format": "url"}
        r = requests.post("https://api.openai.com/v1/images/generations", headers=headers, json=body, timeout=60)
        if r.ok:
            img_r = requests.get(r.json()["data"][0]["url"], timeout=60)
            with open(output_path, "wb") as f:
                f.write(img_r.content)
            corrigir_orientacao(output_path)
            return
        erro = r.json().get("error", {}).get("message", "")
        if ("safety" in erro.lower() or "content" in erro.lower()) and tentativa < 2:
            prompt = suavizar_prompt(prompt, api_key)
            continue
        raise Exception(f"OpenAI erro {r.status_code}: {erro}")

def gerar_imagem_replicate(prompt, api_key, output_path):
    headers = {"Authorization": f"Token {api_key}", "Content-Type": "application/json"}
    body = {"version": "7762fd07cf82c948538e41f63f77d685e02b063e37291fae01d8e3b4a8e9b8e0",
            "input": {"prompt": prompt, "width": 1024, "height": 1024, "num_outputs": 1}}
    r = requests.post("https://api.replicate.com/v1/predictions", headers=headers, json=body, timeout=30)
    r.raise_for_status()
    pid = r.json()["id"]
    for _ in range(60):
        import time; time.sleep(3)
        r2 = requests.get(f"https://api.replicate.com/v1/predictions/{pid}", headers=headers)
        d = r2.json()
        if d["status"] == "succeeded":
            img_r = requests.get(d["output"][0], timeout=30)
            with open(output_path, "wb") as f: f.write(img_r.content)
            return
        if d["status"] == "failed":
            raise Exception("Replicate falhou: " + str(d.get("error")))
    raise Exception("Timeout Replicate")

def gerar_imagem(prompt, user, output_path, estilo="", usar_banco=False):
    if user.provider == "openai":
        gerar_imagem_openai(prompt, user.api_key, user.image_size, user.quality, output_path, modelo="gpt-image-1")
    elif user.provider == "replicate":
        gerar_imagem_replicate(prompt, user.api_key, output_path)
    else:
        raise Exception("Configure sua chave de API de imagens no perfil")
    salvar_no_banco(prompt, estilo, output_path, tipo="imagem")

def gerar_srt(blocos, srt_path):
    def fmt(s):
        h=int(s//3600); m=int((s%3600)//60); sec=s%60
        return f"{h:02d}:{m:02d}:{sec:06.3f}".replace(".",",")
    with open(srt_path, "w", encoding="utf-8") as f:
        for b in blocos:
            f.write(f"{b['index']}\n{fmt(b['inicio'])} --> {fmt(b['fim'])}\n{b['texto']}\n\n")

def gerar_srt_palavras(audio_path, srt_path):
    """Gera SRT com uma palavra por vez sincronizada com o áudio usando Whisper"""
    try:
        print(f"[SRT] Gerando legendas palavra por palavra de: {audio_path}")
        if not os.path.exists(audio_path):
            print(f"[SRT] Audio nao encontrado: {audio_path}")
            return False
        model = get_whisper_model()
        resultado = model.transcribe(audio_path, word_timestamps=True, fp16=False)
        def fmt(s):
            h=int(s//3600); m=int((s%3600)//60); sec=s%60
            return f"{h:02d}:{m:02d}:{sec:06.3f}".replace(".",",")
        idx = 1
        with open(srt_path, "w", encoding="utf-8") as f:
            for seg in resultado.get("segments", []):
                for palavra in seg.get("words", []):
                    word = palavra.get("word", "").strip()
                    if not word:
                        continue
                    start = palavra.get("start", 0)
                    end = palavra.get("end", start + 0.3)
                    f.write(f"{idx}\n{fmt(start)} --> {fmt(end)}\n{word}\n\n")
                    idx += 1
        print(f"[SRT] Gerado com {idx-1} palavras em: {srt_path}")
        return idx > 1
    except Exception as e:
        print(f"[SRT] Erro ao gerar SRT por palavras: {e}")
        return False

def montar_video(imagens, audio_path, output_path, legenda_cfg=None):
    fps = 25
    try:
        im = Image.open(imagens[0]["path"])
        w, h = im.size
        im.close()
    except:
        w, h = 1024, 1792
    sw, sh = int(w * 1.15), int(h * 1.15)
    inputs = []
    filtros = []
    partes = []
    for i, img in enumerate(imagens):
        dur = img["duracao"]
        n = max(int(dur * fps), fps)
        inputs += ["-loop", "1", "-t", str(dur + 1), "-i", os.path.abspath(img["path"])]
        zooms = [
            f"zoompan=z='min(zoom+0.0008,1.12)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={n}:s={w}x{h}:fps={fps}",
            f"zoompan=z='if(lte(zoom,1.0),1.12,max(1.001,zoom-0.0008))':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={n}:s={w}x{h}:fps={fps}",
            f"zoompan=z='1.08':x='if(lte(on,1),0,min(x+0.4,iw-iw/zoom))':y='ih/2-(ih/zoom/2)':d={n}:s={w}x{h}:fps={fps}",
            f"zoompan=z='1.08':x='if(lte(on,1),iw,max(x-0.4,0))':y='ih/2-(ih/zoom/2)':d={n}:s={w}x{h}:fps={fps}",
        ]
        # trim força a duração exata, evitando que zoompan gere frames extras
        filtros.append(f"[{i}:v]scale={sw}:{sh},{zooms[i%4]},trim=duration={dur},setpts=PTS-STARTPTS[v{i}]")
        partes.append(f"[v{i}]")
    filtros.append("".join(partes) + f"concat=n={len(imagens)}:v=1:a=0[vout]")
    saida_v = "[vout]"
    if legenda_cfg and legenda_cfg.get("ativo"):
        srt_path = output_path.replace(".mp4", ".srt")
        # Se tem áudio, gera legenda palavra por palavra sincronizada
        if audio_path and os.path.exists(audio_path):
            print(f"[LEGENDA] Usando audio para legendas palavra por palavra: {audio_path}")
            if not gerar_srt_palavras(audio_path, srt_path):
                print(f"[LEGENDA] Fallback para legenda por cena")
                gerar_srt(imagens, srt_path)  # fallback pra legenda por cena
        else:
            print(f"[LEGENDA] Sem audio, usando legenda por cena")
            gerar_srt(imagens, srt_path)
        fonte = legenda_cfg.get("fonte", "Arial")
        cor = legenda_cfg.get("cor", "&H00FFFFFF")
        tam = legenda_cfg.get("tamanho", "18")
        pos = legenda_cfg.get("posicao", "2")
        sombra = "1" if legenda_cfg.get("sombra", True) else "0"
        srt_esc = os.path.abspath(srt_path)
        filtros.append(f"[vout]subtitles={srt_esc}:force_style='FontName={fonte},FontSize={tam},PrimaryColour={cor},Alignment={pos},Shadow={sombra},Bold=1'[vfinal]")
        saida_v = "[vfinal]"
    fc = ";".join(filtros)
    cmd = ["ffmpeg", "-y"] + inputs
    if audio_path and os.path.exists(audio_path):
        cmd += ["-i", audio_path]
    cmd += ["-filter_complex", fc, "-map", saida_v]
    if audio_path and os.path.exists(audio_path):
        cmd += ["-map", f"{len(imagens)}:a", "-c:a", "aac", "-b:a", "192k", "-shortest"]
    cmd += ["-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p", output_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise Exception(f"FFmpeg erro: {result.stderr[-800:]}")

def limpar_job(job_dir):
    try:
        if os.path.exists(job_dir): shutil.rmtree(job_dir)
    except: pass

def dividir_roteiro(texto, api_key):
    # Texto muito curto (1 frase simples) = não divide
    if len(texto) < 30:
        return [texto.strip()]

    # Limitar cenas: ~1 cena a cada 40 caracteres, mínimo 2, máximo 12
    max_cenas = max(2, min(12, len(texto) // 40))

    prompts = load_prompts()
    try:
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        system = prompts.get("dividir", DEFAULT_PROMPTS["dividir"])
        system += f"\n\nIMPORTANT: Create a MAXIMUM of {max_cenas} scenes. Each scene must be a meaningful story beat, NOT individual words. If the text describes 2-3 events, create 2-3 scenes. Do NOT over-split."
        body = {"model": "gpt-4o-mini", "messages": [
            {"role": "system", "content": system}, {"role": "user", "content": texto}
        ], "max_tokens": 300}
        r = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=body, timeout=30)
        if r.ok:
            resultado = r.json()["choices"][0]["message"]["content"].strip()
            linhas = [l.strip() for l in resultado.split("\n") if l.strip()]
            # Garantir que não excede o máximo
            if len(linhas) > max_cenas:
                linhas = linhas[:max_cenas]
            if len(linhas) >= 1:
                return linhas
    except: pass
    return [l.strip() for l in texto.replace(",", "\n").replace(".", "\n").split("\n") if l.strip()] or [texto.strip()]

def gerar_storyboard(job_id, user_id, texto_manual, estilo, melhorar_prompts, usar_banco=False, cenas_preenchidas=None):
    if cenas_preenchidas is None:
        cenas_preenchidas = {}
    with app.app_context():
        try:
            resetar_banco_usadas()
            user = User.query.get(user_id)
            jobs[job_id] = {"status": "processando", "progresso": "Analisando roteiro...", "total": 0, "atual": 0}
            sb_dir = os.path.join(STORYBOARD_FOLDER, job_id)
            os.makedirs(sb_dir, exist_ok=True)
            if user.provider == "openai":
                linhas = dividir_roteiro(texto_manual, user.api_key)
            else:
                linhas = [l.strip() for l in texto_manual.replace(",", "\n").replace(".", "\n").split("\n") if l.strip()]

            total = len(linhas)
            # Contar quantas cenas precisam ser geradas (excluir preenchidas)
            cenas_a_gerar = [i for i in range(total) if str(i+1) not in cenas_preenchidas]
            creditos_necessarios = len(cenas_a_gerar) * CREDITOS_POR_IMAGEM
            if not user.tem_creditos(creditos_necessarios):
                jobs[job_id] = {"status": "erro", "progresso": f"Créditos insuficientes. Necessário: {creditos_necessarios}, disponível: {user.creditos}", "total": 0, "atual": 0}
                return

            jobs[job_id]["total"] = total
            blocos = []
            roteiro_completo = texto_manual

            # Copiar cenas preenchidas do banco
            for idx_str, banco_path in cenas_preenchidas.items():
                idx = int(idx_str) - 1
                if idx < total:
                    img_path = os.path.join(sb_dir, f"{idx+1:03d}.png")
                    src = os.path.join(BANCO_IMG_FOLDER, banco_path) if not os.path.isabs(banco_path) else banco_path
                    if os.path.exists(src):
                        shutil.copy(src, img_path)
                        blocos.append({"index": idx+1, "texto": linhas[idx], "img": f"{idx+1:03d}.png"})

            # Extrair ficha de personagens pra consistência visual
            ficha = ""
            if melhorar_prompts and user.provider == "openai" and cenas_a_gerar:
                jobs[job_id]["progresso"] = "Analisando personagens..."
                ficha = extrair_personagens(texto_manual, user.api_key)

            def gerar_bloco(i_linha):
                linha = linhas[i_linha]
                if melhorar_prompts and user.provider == "openai":
                    prompt_final = melhorar_prompt(linha, estilo, user.api_key, roteiro_completo, ficha)
                else:
                    prompt_final = f"{linha}, {estilo}" if estilo else linha
                img_path = os.path.join(sb_dir, f"{i_linha+1:03d}.png")
                gerar_imagem(prompt_final, user, img_path, estilo)
                blocos.append({"index": i_linha+1, "texto": linha, "img": f"{i_linha+1:03d}.png"})
                jobs[job_id]["atual"] = len(blocos)
                jobs[job_id]["progresso"] = f"Gerando imagem {len(blocos)} de {total}..."

            if cenas_a_gerar:
                with ThreadPoolExecutor(max_workers=3) as executor:
                    list(executor.map(gerar_bloco, cenas_a_gerar))

            # Gastar créditos apenas pelas cenas geradas
            if cenas_a_gerar:
                user.gastar_creditos(len(cenas_a_gerar) * CREDITOS_POR_IMAGEM)
                db.session.commit()

            blocos.sort(key=lambda x: x["index"])
            sb_data = {"blocos": blocos, "estilo": estilo, "dir": sb_dir}
            with open(os.path.join(sb_dir, "storyboard.json"), "w") as f:
                json.dump(sb_data, f)
            jobs[job_id] = {"status": "storyboard_pronto", "progresso": "Storyboard pronto", "total": total, "atual": total, "blocos": blocos, "sb_id": job_id}
        except Exception as e:
            jobs[job_id] = {"status": "erro", "progresso": str(e), "total": 0, "atual": 0}

def finalizar_video(job_id, user_id, sb_id, voice_id, modo_video, legenda_cfg, intervalo, animar_ia=False):
    with app.app_context():
        try:
            user = User.query.get(user_id)
            jobs[job_id] = {"status": "processando", "progresso": "Finalizando video...", "total": 0, "atual": 0}
            sb_dir = os.path.join(STORYBOARD_FOLDER, sb_id)
            with open(os.path.join(sb_dir, "storyboard.json")) as f:
                sb_data = json.load(f)
            blocos = sb_data["blocos"]
            job_dir = os.path.join(OUTPUT_FOLDER, job_id)
            os.makedirs(job_dir, exist_ok=True)
            audio_final_path = None

            if user.minimax_key and voice_id:
                jobs[job_id]["progresso"] = "Narrando frase por frase..."
                audios = []
                imagens = []
                t = 0
                for i, bloco in enumerate(blocos):
                    frase_path = os.path.join(job_dir, f"frase_{i:03d}.mp3")
                    gerar_audio_minimax(bloco["texto"], user.minimax_key, user.minimax_group_id, voice_id, frase_path)
                    frase_audio = AudioSegment.from_file(frase_path)
                    if modo_video == "shorts":
                        frase_limpa = os.path.join(job_dir, f"frase_{i:03d}_limpa.mp3")
                        remover_silencio(frase_path, frase_limpa)
                        frase_audio = AudioSegment.from_file(frase_limpa)
                    dur_frase = len(frase_audio) / 1000
                    audios.append(frase_audio)
                    # 1 imagem por cena, duração = duração da frase narrada
                    img_src = os.path.join(sb_dir, bloco["img"])
                    img_dst = os.path.join(job_dir, f"{i+1:04d}.png")
                    shutil.copy(img_src, img_dst)
                    imagens.append({"index": i+1, "path": img_dst, "duracao": round(dur_frase, 2),
                                    "inicio": round(t, 2), "fim": round(t + dur_frase, 2),
                                    "texto": bloco["texto"]})
                    t += dur_frase
                audio_completo = audios[0]
                for a in audios[1:]:
                    audio_completo += a
                audio_final_path = os.path.join(job_dir, "narracao.mp3")
                audio_completo.export(audio_final_path, format="mp3")
            else:
                # Sem narração: 1 imagem por cena, duração = intervalo
                imagens = []
                t = 0
                for i, bloco in enumerate(blocos):
                    img_src = os.path.join(sb_dir, bloco["img"])
                    img_dst = os.path.join(job_dir, f"{i+1:04d}.png")
                    shutil.copy(img_src, img_dst)
                    imagens.append({"index": i+1, "path": img_dst, "duracao": intervalo,
                                    "inicio": round(t, 2), "fim": round(t + intervalo, 2),
                                    "texto": bloco["texto"]})
                    t += intervalo

            jobs[job_id]["total"] = len(imagens)
            jobs[job_id]["atual"] = len(imagens)

            # Animar imagens com IA (MiniMax Video)
            import sys
            sys.stderr.write(f"[VIDEO] animar_ia={animar_ia}, minimax_key={'SIM' if user.minimax_key else 'NAO'}\n")
            sys.stderr.flush()
            if animar_ia and user.minimax_key:
                n_cenas = len(imagens)
                tempo_est = max(3, min(8, n_cenas))  # 3-8 min estimado (paralelo)
                jobs[job_id]["progresso"] = f"Animando {n_cenas} cenas com IA em paralelo. Tempo estimado: ~{tempo_est} minutos..."
                clipes_video = [None] * n_cenas

                def animar_cena(i):
                    img = imagens[i]
                    clipe_path = os.path.join(job_dir, f"clipe_{i+1:04d}.mp4")
                    try:
                        gerar_video_minimax(img["path"], img["texto"], user.minimax_key, clipe_path)
                        clipes_video[i] = clipe_path
                        # Salvar cena animada no banco
                        salvar_no_banco(img["texto"], sbEstilo if 'sbEstilo' in dir() else "", clipe_path, tipo="video", categoria="cena_animada")
                        jobs[job_id]["atual"] = sum(1 for c in clipes_video if c is not None)
                        jobs[job_id]["progresso"] = f"Animando cenas... {jobs[job_id]['atual']}/{n_cenas} prontas (~{tempo_est} min)"
                    except Exception as e:
                        sys.stderr.write(f"[ANIMAR] Erro na cena {i+1}: {e}\n")
                        sys.stderr.flush()
                        clipes_video[i] = None

                with ThreadPoolExecutor(max_workers=min(n_cenas, 5)) as executor:
                    list(executor.map(animar_cena, range(n_cenas)))

                # Se pelo menos 1 clipe foi gerado, concatena os vídeos
                if any(clipes_video):
                    jobs[job_id]["progresso"] = "Juntando clipes animados..."
                    # Criar lista de concat
                    concat_path = os.path.join(job_dir, "concat_list.txt")
                    with open(concat_path, "w") as f:
                        for cp in clipes_video:
                            if cp and os.path.exists(cp):
                                f.write(f"file '{os.path.abspath(cp)}'\n")
                    video_path = os.path.join(OUTPUT_FOLDER, f"{job_id}.mp4")
                    # Concatenar clipes
                    cmd_concat = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_path]
                    if audio_final_path and os.path.exists(audio_final_path):
                        cmd_concat += ["-i", audio_final_path, "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest"]
                    else:
                        cmd_concat += ["-c:v", "copy"]

                    # Adicionar legendas se ativo
                    if legenda_cfg and legenda_cfg.get("ativo") and audio_final_path:
                        srt_path = video_path.replace(".mp4", ".srt")
                        gerar_srt_palavras(audio_final_path, srt_path)
                        fonte = legenda_cfg.get("fonte", "Arial")
                        cor = legenda_cfg.get("cor", "&H00FFFFFF")
                        tam = legenda_cfg.get("tamanho", "18")
                        pos = legenda_cfg.get("posicao", "2")
                        sombra = "1" if legenda_cfg.get("sombra", True) else "0"
                        # Precisa re-encode pra adicionar legendas
                        video_temp = os.path.join(job_dir, "temp_concat.mp4")
                        cmd_concat += [video_temp]
                        result = subprocess.run(cmd_concat, capture_output=True, text=True)
                        if result.returncode != 0:
                            raise Exception(f"FFmpeg concat erro: {result.stderr[-500:]}")
                        srt_esc = os.path.abspath(srt_path)
                        cmd_sub = ["ffmpeg", "-y", "-i", video_temp]
                        if audio_final_path and os.path.exists(audio_final_path):
                            cmd_sub += ["-i", audio_final_path]
                        cmd_sub += ["-vf", f"subtitles={srt_esc}:force_style='FontName={fonte},FontSize={tam},PrimaryColour={cor},Alignment={pos},Shadow={sombra},Bold=1'",
                                    "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p"]
                        if audio_final_path and os.path.exists(audio_final_path):
                            cmd_sub += ["-c:a", "aac", "-b:a", "192k", "-shortest"]
                        cmd_sub += [video_path]
                        result = subprocess.run(cmd_sub, capture_output=True, text=True)
                        if result.returncode != 0:
                            raise Exception(f"FFmpeg legendas erro: {result.stderr[-500:]}")
                    else:
                        cmd_concat += [video_path]
                        result = subprocess.run(cmd_concat, capture_output=True, text=True)
                        if result.returncode != 0:
                            raise Exception(f"FFmpeg concat erro: {result.stderr[-500:]}")
                else:
                    # Nenhum clipe gerado, fallback pra montagem normal
                    jobs[job_id]["progresso"] = "Montando video (sem animação)..."
                    video_path = os.path.join(OUTPUT_FOLDER, f"{job_id}.mp4")
                    montar_video(imagens, audio_final_path, video_path, legenda_cfg)
            else:
                jobs[job_id]["progresso"] = "Montando video..."
                video_path = os.path.join(OUTPUT_FOLDER, f"{job_id}.mp4")
                montar_video(imagens, audio_final_path, video_path, legenda_cfg)

            jobs[job_id]["progresso"] = "Compactando..."
            zip_path = os.path.join(OUTPUT_FOLDER, f"{job_id}.zip")
            with zipfile.ZipFile(zip_path, "w") as zf:
                for img in imagens:
                    zf.write(img["path"], os.path.basename(img["path"]))
                rp = os.path.join(job_dir, "roteiro.txt")
                with open(rp, "w", encoding="utf-8") as f:
                    for img in imagens:
                        f.write(f"Imagem {img['index']:03d} [{img['inicio']}s-{img['fim']}s]: {img['texto']}\n")
                zf.write(rp, "roteiro.txt")
                if audio_final_path and os.path.exists(audio_final_path):
                    zf.write(audio_final_path, "narracao.mp3")

            criacao = Criacao(user_id=user_id, job_id=job_id, nome=f"Criacao {datetime.now().strftime('%d/%m/%Y %H:%M')}",
                              total_imagens=len(imagens), zip_path=zip_path, video_path=video_path)
            db.session.add(criacao)
            db.session.commit()
            limpar_job(job_dir)
            jobs[job_id] = {"status": "pronto", "progresso": "Concluido", "total": len(imagens), "atual": len(imagens), "zip": zip_path, "video": video_path}
        except Exception as e:
            jobs[job_id] = {"status": "erro", "progresso": str(e), "total": 0, "atual": 0}

# ── Rotas Auth ────────────────────────────────────────────
@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        data = request.json
        user = User.query.filter_by(email=data["email"]).first()
        if user and check_password_hash(user.senha, data["senha"]):
            login_user(user)
            return jsonify({"ok": True})
        return jsonify({"erro": "Email ou senha incorretos"}), 401
    return render_template("login.html")

@app.route("/cadastro", methods=["GET", "POST"])
def cadastro():
    if request.method == "POST":
        data = request.json
        if User.query.filter_by(email=data["email"]).first():
            return jsonify({"erro": "Email ja cadastrado"}), 400
        user = User(email=data["email"], nome=data["nome"], senha=generate_password_hash(data["senha"]))
        db.session.add(user)
        db.session.commit()
        login_user(user)
        return jsonify({"ok": True})
    return render_template("cadastro.html")

@app.route("/esqueci_senha", methods=["POST"])
def esqueci_senha():
    data = request.json
    email = data.get("email", "").strip()
    user = User.query.filter_by(email=email).first()
    if not user:
        return jsonify({"erro": "Email nao encontrado"}), 404
    nova_senha = uuid.uuid4().hex[:8]
    user.senha = generate_password_hash(nova_senha)
    db.session.commit()
    return jsonify({"ok": True, "nova_senha": nova_senha})

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))

# ── Rotas Stripe ─────────────────────────────────────────
@app.route("/planos")
@login_required
def planos():
    return jsonify({
        "planos": {k: {**v, "key": k} for k, v in PLANOS_STRIPE.items()},
        "avulsos": {k: {**v, "key": k} for k, v in PACOTES_AVULSO.items()},
        "creditos_por_imagem": CREDITOS_POR_IMAGEM,
        "meu_plano": current_user.plano or "",
        "meus_creditos": current_user.creditos,
    })

@app.route("/checkout", methods=["POST"])
@login_required
def checkout():
    data = request.json
    price_id = data.get("price_id")

    if price_id not in PRICE_MAP:
        return jsonify({"erro": "Plano invalido"}), 400

    plano_info = PRICE_MAP[price_id]

    # Criar ou reutilizar customer na Stripe
    if not current_user.stripe_customer_id:
        customer = stripe.Customer.create(
            email=current_user.email,
            name=current_user.nome,
            metadata={"user_id": str(current_user.id)}
        )
        current_user.stripe_customer_id = customer.id
        db.session.commit()

    # Criar sessão de checkout
    checkout_params = {
        "customer": current_user.stripe_customer_id,
        "payment_method_types": ["card"],
        "line_items": [{"price": price_id, "quantity": 1}],
        "success_url": request.host_url + "pagamento_sucesso?session_id={CHECKOUT_SESSION_ID}",
        "cancel_url": request.host_url + "dashboard",
        "locale": "pt-BR",
        "custom_text": {
            "submit": {"message": "Seu acesso será liberado imediatamente após o pagamento."},
        },
        "metadata": {
            "user_id": str(current_user.id),
            "price_id": price_id,
            "plano_key": plano_info.get("key", ""),
            "creditos": str(plano_info.get("creditos", 0)),
            "tipo": plano_info.get("tipo", ""),
        }
    }

    if plano_info["tipo"] == "assinatura":
        checkout_params["mode"] = "subscription"
        checkout_params["subscription_data"] = {
            "metadata": {
                "user_id": str(current_user.id),
                "plano_key": plano_info.get("key", ""),
                "creditos": str(plano_info.get("creditos", 0)),
            }
        }
    else:
        checkout_params["mode"] = "payment"

    session = stripe.checkout.Session.create(**checkout_params)
    return jsonify({"checkout_url": session.url})

@app.route("/portal_cliente")
@login_required
def portal_cliente():
    if not current_user.stripe_customer_id:
        return redirect(url_for("dashboard"))
    session = stripe.billing_portal.Session.create(
        customer=current_user.stripe_customer_id,
        return_url=request.host_url + "dashboard",
    )
    return redirect(session.url)

@app.route("/pagamento_sucesso")
@login_required
def pagamento_sucesso():
    session_id = request.args.get("session_id")
    if session_id:
        try:
            session = stripe.checkout.Session.retrieve(session_id)
            meta = session.metadata
            user_id = int(meta.get("user_id", 0))
            if user_id == current_user.id and session.payment_status == "paid":
                tipo = meta.get("tipo", "")
                creditos = int(meta.get("creditos", 0))
                plano_key = meta.get("plano_key", "")

                if tipo == "assinatura":
                    current_user.plano = plano_key
                    current_user.creditos = creditos  # Reset mensal
                elif tipo == "avulso":
                    current_user.creditos += creditos  # Soma aos existentes

                db.session.commit()
        except Exception as e:
            print(f"Erro ao processar pagamento: {e}")
    return redirect(url_for("dashboard"))

@app.route("/stripe_webhook", methods=["POST"])
def stripe_webhook():
    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature")

    if STRIPE_WEBHOOK_SECRET:
        try:
            event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
        except (ValueError, stripe.error.SignatureVerificationError):
            return jsonify({"erro": "Webhook invalido"}), 400
    else:
        event = json.loads(payload)

    # Renovação mensal da assinatura
    if event["type"] == "invoice.payment_succeeded":
        invoice = event["data"]["object"]
        customer_id = invoice.get("customer")
        if customer_id:
            user = User.query.filter_by(stripe_customer_id=customer_id).first()
            if user and user.plano and user.plano in PLANOS_STRIPE:
                user.creditos = PLANOS_STRIPE[user.plano]["creditos"]
                db.session.commit()

    # Assinatura cancelada
    elif event["type"] == "customer.subscription.deleted":
        sub = event["data"]["object"]
        customer_id = sub.get("customer")
        if customer_id:
            user = User.query.filter_by(stripe_customer_id=customer_id).first()
            if user:
                user.plano = ""
                db.session.commit()

    # Checkout completado (backup do pagamento_sucesso)
    elif event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        meta = session.get("metadata", {})
        user_id = int(meta.get("user_id", 0))
        if user_id and session.get("payment_status") == "paid":
            user = User.query.get(user_id)
            if user:
                tipo = meta.get("tipo", "")
                creditos = int(meta.get("creditos", 0))
                plano_key = meta.get("plano_key", "")
                if tipo == "assinatura":
                    user.plano = plano_key
                    user.creditos = creditos
                elif tipo == "avulso":
                    user.creditos += creditos
                db.session.commit()

    return jsonify({"ok": True})

@app.route("/cancelar_assinatura", methods=["POST"])
@login_required
def cancelar_assinatura():
    if not current_user.stripe_customer_id:
        return jsonify({"erro": "Nenhuma assinatura encontrada"}), 400
    try:
        subs = stripe.Subscription.list(customer=current_user.stripe_customer_id, status="active", limit=1)
        if subs.data:
            stripe.Subscription.cancel(subs.data[0].id)
            current_user.plano = ""
            db.session.commit()
            return jsonify({"ok": True})
        return jsonify({"erro": "Nenhuma assinatura ativa"}), 400
    except Exception as e:
        return jsonify({"erro": str(e)}), 500

@app.route("/meus_creditos")
@login_required
def meus_creditos():
    return jsonify({
        "creditos": current_user.creditos,
        "plano": current_user.plano,
        "plano_nome": PLANOS_STRIPE.get(current_user.plano, {}).get("nome", "Sem plano"),
        "is_admin": current_user.is_admin,
    })

# ── Rotas Dashboard ──────────────────────────────────────
@app.route("/dashboard")
@login_required
def dashboard():
    criacoes = Criacao.query.filter_by(user_id=current_user.id).order_by(Criacao.criado_em.desc()).all()
    vozes_clonadas = current_user.get_vozes_clonadas()
    return render_template("dashboard.html", user=current_user, criacoes=criacoes, vozes=VOZES_MINIMAX,
                           vozes_clonadas=vozes_clonadas, planos=PLANOS_STRIPE, avulsos=PACOTES_AVULSO,
                           creditos_por_imagem=CREDITOS_POR_IMAGEM)

@app.route("/perfil", methods=["GET", "POST"])
@login_required
def perfil():
    if request.method == "POST":
        data = request.json
        current_user.provider = data.get("provider", "")
        current_user.api_key = data.get("api_key", "")
        current_user.image_size = data.get("image_size", "1024x1024")
        current_user.quality = data.get("quality", "standard")
        current_user.minimax_key = data.get("minimax_key", "")
        current_user.minimax_group_id = data.get("minimax_group_id", "")
        db.session.commit()
        return jsonify({"ok": True})
    return jsonify({"provider": current_user.provider, "api_key": current_user.api_key,
                    "image_size": current_user.image_size, "quality": current_user.quality,
                    "minimax_key": current_user.minimax_key, "minimax_group_id": current_user.minimax_group_id})

@app.route("/mudar_senha", methods=["POST"])
@login_required
def mudar_senha():
    data = request.json
    senha_atual = data.get("senha_atual", "")
    nova_senha = data.get("nova_senha", "")
    if not check_password_hash(current_user.senha, senha_atual):
        return jsonify({"erro": "Senha atual incorreta"}), 400
    if len(nova_senha) < 6:
        return jsonify({"erro": "Nova senha deve ter pelo menos 6 caracteres"}), 400
    current_user.senha = generate_password_hash(nova_senha)
    db.session.commit()
    return jsonify({"ok": True})

# ── Admin ────────────────────────────────────────────────
@app.route("/admin")
@login_required
def admin_panel():
    if not current_user.is_admin:
        return redirect(url_for("dashboard"))
    users = User.query.all()
    prompts = load_prompts()
    conn = sqlite3.connect('instance/veo3.db')
    try:
        total_imgs = conn.execute("SELECT COUNT(*) FROM banco_imagens WHERE tipo='imagem' OR tipo IS NULL").fetchone()[0]
    except:
        total_imgs = 0
    try:
        total_videos = conn.execute("SELECT COUNT(*) FROM banco_imagens WHERE tipo='video'").fetchone()[0]
    except:
        total_videos = 0
    conn.close()
    total_criacoes = Criacao.query.count()
    total_creditos = sum(u.creditos for u in users)
    users_com_plano = sum(1 for u in users if u.plano)
    from datetime import timedelta
    hoje = datetime.utcnow()
    users_recentes = sum(1 for u in users if (hoje - u.criado_em).days <= 7)
    return render_template("admin.html", users=users, prompts=prompts, total_imgs=total_imgs,
                           total_criacoes=total_criacoes, total_videos=total_videos,
                           total_creditos=total_creditos, users_com_plano=users_com_plano,
                           users_recentes=users_recentes, planos=PLANOS_STRIPE)

@app.route("/admin/toggle_admin", methods=["POST"])
@login_required
def admin_toggle():
    if not current_user.is_admin:
        return jsonify({"erro": "Sem permissao"}), 403
    user_id = request.json.get("user_id")
    user = User.query.get(user_id)
    if user:
        user.is_admin = not user.is_admin
        db.session.commit()
        return jsonify({"ok": True, "is_admin": user.is_admin})
    return jsonify({"erro": "Usuario nao encontrado"}), 404

@app.route("/admin/mudar_senha", methods=["POST"])
@login_required
def admin_mudar_senha():
    if not current_user.is_admin:
        return jsonify({"erro": "Sem permissao"}), 403
    user_id = request.json.get("user_id")
    nova_senha = request.json.get("nova_senha", "").strip()
    if not nova_senha or len(nova_senha) < 6:
        return jsonify({"erro": "Senha deve ter pelo menos 6 caracteres"}), 400
    user = User.query.get(user_id)
    if user:
        user.senha = generate_password_hash(nova_senha)
        db.session.commit()
        return jsonify({"ok": True})
    return jsonify({"erro": "Usuario nao encontrado"}), 404

@app.route("/admin/deletar_usuario", methods=["POST"])
@login_required
def admin_deletar_usuario():
    if not current_user.is_admin:
        return jsonify({"erro": "Sem permissao"}), 403
    user_id = request.json.get("user_id")
    if user_id == current_user.id:
        return jsonify({"erro": "Nao pode deletar a si mesmo"}), 400
    user = User.query.get(user_id)
    if user:
        Criacao.query.filter_by(user_id=user_id).delete()
        db.session.delete(user)
        db.session.commit()
        return jsonify({"ok": True})
    return jsonify({"erro": "Usuario nao encontrado"}), 404

@app.route("/admin/salvar_prompts", methods=["POST"])
@login_required
def admin_salvar_prompts():
    if not current_user.is_admin:
        return jsonify({"erro": "Sem permissao"}), 403
    data = request.json
    save_prompts(data)
    return jsonify({"ok": True})

@app.route("/admin/banco_imagens")
@login_required
def admin_banco_imagens():
    if not current_user.is_admin:
        return jsonify({"erro": "Sem permissao"}), 403
    page = int(request.args.get("page", 1))
    per_page = 20
    conn = sqlite3.connect('instance/veo3.db')
    total = conn.execute("SELECT COUNT(*) FROM banco_imagens").fetchone()[0]
    rows = conn.execute("SELECT id, prompt, estilo, path, criado_em FROM banco_imagens ORDER BY id DESC LIMIT ? OFFSET ?",
                        (per_page, (page-1)*per_page)).fetchall()
    conn.close()
    imgs = [{"id": r[0], "prompt": r[1], "estilo": r[2], "path": r[3], "criado_em": r[4]} for r in rows]
    return jsonify({"imgs": imgs, "total": total, "page": page, "pages": math.ceil(total/per_page)})

@app.route("/admin/deletar_imagem", methods=["POST"])
@login_required
def admin_deletar_imagem():
    if not current_user.is_admin:
        return jsonify({"erro": "Sem permissao"}), 403
    img_id = request.json.get("id")
    conn = sqlite3.connect('instance/veo3.db')
    row = conn.execute("SELECT path FROM banco_imagens WHERE id=?", (img_id,)).fetchone()
    if row and os.path.exists(row[0]):
        os.remove(row[0])
    conn.execute("DELETE FROM banco_imagens WHERE id=?", (img_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.route("/admin/dar_creditos", methods=["POST"])
@login_required
def admin_dar_creditos():
    if not current_user.is_admin:
        return jsonify({"erro": "Sem permissao"}), 403
    user_id = request.json.get("user_id")
    qtd = int(request.json.get("creditos", 0))
    user = User.query.get(user_id)
    if user:
        user.creditos += qtd
        db.session.commit()
        return jsonify({"ok": True, "creditos": user.creditos})
    return jsonify({"erro": "Usuario nao encontrado"}), 404

@app.route("/admin/definir_plano", methods=["POST"])
@login_required
def admin_definir_plano():
    if not current_user.is_admin:
        return jsonify({"erro": "Sem permissao"}), 403
    user_id = request.json.get("user_id")
    plano = request.json.get("plano", "")
    user = User.query.get(user_id)
    if user:
        user.plano = plano
        if plano in PLANOS_STRIPE:
            user.creditos = PLANOS_STRIPE[plano]["creditos"]
        db.session.commit()
        return jsonify({"ok": True})
    return jsonify({"erro": "Usuario nao encontrado"}), 404

@app.route("/banco_img/<path:filename>")
@login_required
def banco_img_file(filename):
    return send_file(os.path.join(BANCO_IMG_FOLDER, filename))

@app.route("/buscar_banco", methods=["POST"])
@login_required
def buscar_banco():
    termo = request.json.get("termo", "").lower().strip()
    tipo_filtro = request.json.get("tipo", "")  # "imagem", "video", ou "" (todos)
    estilo = request.json.get("estilo", "")
    conn = sqlite3.connect('instance/veo3.db')
    query = "SELECT id, prompt, path, estilo, tipo, categoria, descricao FROM banco_imagens WHERE 1=1"
    params = []
    if termo:
        query += " AND (tags LIKE ? OR prompt LIKE ? OR descricao LIKE ?)"
        params += [f"%{termo}%", f"%{termo}%", f"%{termo}%"]
    if tipo_filtro:
        query += " AND tipo = ?"
        params.append(tipo_filtro)
    if estilo:
        query += " AND estilo = ?"
        params.append(estilo)
    query += " ORDER BY id DESC LIMIT 30"
    try:
        rows = conn.execute(query, params).fetchall()
    except:
        # Fallback se colunas novas não existem ainda
        rows = conn.execute("SELECT id, prompt, path, estilo, 'imagem', '', prompt FROM banco_imagens ORDER BY id DESC LIMIT 30").fetchall()
    conn.close()
    imgs = []
    for r in rows:
        if os.path.exists(r[2]):
            imgs.append({
                "id": r[0], "prompt": r[1], "path": os.path.basename(r[2]),
                "estilo": r[3] or "", "tipo": r[4] or "imagem",
                "categoria": r[5] or "", "descricao": r[6] or r[1]
            })
    return jsonify({"imgs": imgs})

@app.route("/usar_banco_cena", methods=["POST"])
@login_required
def usar_banco_cena():
    sb_id = request.form.get("sb_id")
    index = int(request.form.get("index"))
    img_id = int(request.form.get("img_id"))
    sb_dir = os.path.join(STORYBOARD_FOLDER, sb_id)
    sb_path = os.path.join(sb_dir, "storyboard.json")
    with open(sb_path) as f:
        sb_data = json.load(f)
    bloco = sb_data["blocos"][index - 1]
    conn = sqlite3.connect('instance/veo3.db')
    row = conn.execute("SELECT path FROM banco_imagens WHERE id=?", (img_id,)).fetchone()
    conn.close()
    if row and os.path.exists(row[0]):
        shutil.copy(row[0], os.path.join(sb_dir, bloco["img"]))
        return jsonify({"ok": True})
    return jsonify({"erro": "Imagem nao encontrada"}), 404

# ── Rotas Voz ────────────────────────────────────────────
@app.route("/clonar_voz", methods=["POST"])
@login_required
def clonar_voz():
    if not current_user.minimax_key:
        return jsonify({"erro": "Configure a chave MiniMax no perfil"}), 400
    if "audio" not in request.files or not request.files["audio"].filename:
        return jsonify({"erro": "Envie um arquivo de audio"}), 400
    nome_voz = request.form.get("nome_voz", "").strip()
    if not nome_voz:
        return jsonify({"erro": "Digite um nome para a voz"}), 400
    audio = request.files["audio"]
    voice_id = f"user_{current_user.id}_{uuid.uuid4().hex[:8]}"
    caminho = os.path.join(UPLOAD_FOLDER, f"{voice_id}.mp3")
    audio.save(caminho)
    try:
        clonar_voz_minimax(current_user.minimax_key, current_user.minimax_group_id, caminho, voice_id)
        current_user.add_voz_clonada(nome_voz, voice_id)
        db.session.commit()
        os.remove(caminho)
        return jsonify({"ok": True, "voice_id": voice_id, "nome": nome_voz})
    except Exception as e:
        if os.path.exists(caminho): os.remove(caminho)
        return jsonify({"erro": str(e)}), 500

@app.route("/deletar_voz", methods=["POST"])
@login_required
def deletar_voz():
    voice_id = request.json.get("voice_id")
    vozes = current_user.get_vozes_clonadas()
    vozes = [v for v in vozes if v["voice_id"] != voice_id]
    current_user.vozes_clonadas = json.dumps(vozes)
    db.session.commit()
    return jsonify({"ok": True})

# ── Rotas Storyboard ─────────────────────────────────────
@app.route("/dividir_roteiro", methods=["POST"])
@login_required
def dividir_roteiro_route():
    """Divide o roteiro em cenas sem gerar imagens — pra o usuário preencher do banco antes"""
    texto = request.form.get("texto", "").strip()
    if not texto:
        return jsonify({"erro": "Escreva o roteiro"}), 400
    estilo = request.form.get("estilo", "").strip()
    melhorar = request.form.get("melhorar_prompts", "false") == "true"

    if melhorar and current_user.provider == "openai" and current_user.api_key:
        linhas = dividir_roteiro(texto, current_user.api_key)
    else:
        linhas = [l.strip() for l in texto.replace(",", "\n").replace(".", "\n").split("\n") if l.strip()] or [texto.strip()]

    cenas = [{"index": i+1, "texto": l, "preenchida": False} for i, l in enumerate(linhas)]
    return jsonify({"cenas": cenas, "total": len(cenas)})

@app.route("/gerar_storyboard", methods=["POST"])
@login_required
def gerar_storyboard_route():
    if not current_user.api_key or not current_user.provider:
        # Plano api_propria exige chave do usuário
        if current_user.plano == "api_propria" and (not current_user.api_key or not current_user.provider):
            return jsonify({"erro": "No plano API Própria, configure sua chave de API no perfil"}), 400
        # Outros planos sem chave = usa chave da plataforma (futuro) ou exige config
        if not current_user.api_key or not current_user.provider:
            return jsonify({"erro": "Configure sua chave de API de imagens no perfil"}), 400

    # Verificar se tem créditos (checagem básica)
    if not current_user.tem_creditos(CREDITOS_POR_IMAGEM):
        return jsonify({"erro": f"Créditos insuficientes. Você tem {current_user.creditos} créditos. Compre mais na aba Planos."}), 400

    texto = request.form.get("texto", "").strip()
    if not texto:
        return jsonify({"erro": "Escreva o roteiro"}), 400
    estilo = request.form.get("estilo", "").strip()
    melhorar = request.form.get("melhorar_prompts", "false") == "true"
    usar_banco = request.form.get("usar_banco", "false") == "true"
    # Cenas já preenchidas do banco (JSON com índices)
    cenas_preenchidas = request.form.get("cenas_preenchidas", "{}")
    try:
        cenas_preenchidas = json.loads(cenas_preenchidas)
    except:
        cenas_preenchidas = {}

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "aguardando", "progresso": "Na fila...", "total": 0, "atual": 0}
    thread = threading.Thread(target=gerar_storyboard, args=(job_id, current_user.id, texto, estilo, melhorar, False, cenas_preenchidas))
    thread.daemon = True
    thread.start()
    return jsonify({"job_id": job_id})

@app.route("/storyboard_img/<sb_id>/<filename>")
@login_required
def storyboard_img(sb_id, filename):
    return send_file(os.path.join(STORYBOARD_FOLDER, sb_id, filename))

@app.route("/regerar_cena", methods=["POST"])
@login_required
def regerar_cena():
    if not current_user.api_key:
        return jsonify({"erro": "Configure API"}), 400
    # Cobrar crédito por regeneração
    if not current_user.gastar_creditos(CREDITOS_POR_IMAGEM):
        return jsonify({"erro": f"Créditos insuficientes. Necessário: {CREDITOS_POR_IMAGEM}"}), 400
    db.session.commit()

    sb_id = request.form.get("sb_id")
    index = int(request.form.get("index"))
    texto = request.form.get("texto", "").strip()
    estilo = request.form.get("estilo", "").strip()
    melhorar = request.form.get("melhorar_prompts", "false") == "true"
    sb_dir = os.path.join(STORYBOARD_FOLDER, sb_id)
    sb_path = os.path.join(sb_dir, "storyboard.json")
    try:
        with open(sb_path) as f:
            sb_data = json.load(f)
        bloco = sb_data["blocos"][index - 1]
        bloco["texto"] = texto
        if melhorar and current_user.provider == "openai":
            prompt = melhorar_prompt(texto, estilo, current_user.api_key)
        else:
            prompt = f"{texto}, {estilo}" if estilo else texto
        img_path = os.path.join(sb_dir, bloco["img"])
        gerar_imagem(prompt, current_user, img_path, estilo, False)
        with open(sb_path, "w") as f:
            json.dump(sb_data, f)
        return jsonify({"ok": True})
    except Exception as e:
        # Devolver crédito se falhou
        current_user.creditos += CREDITOS_POR_IMAGEM
        db.session.commit()
        return jsonify({"erro": str(e)}), 500

@app.route("/upload_cena", methods=["POST"])
@login_required
def upload_cena():
    sb_id = request.form.get("sb_id")
    index = int(request.form.get("index"))
    if "imagem" not in request.files:
        return jsonify({"erro": "Envie uma imagem"}), 400
    sb_dir = os.path.join(STORYBOARD_FOLDER, sb_id)
    sb_path = os.path.join(sb_dir, "storyboard.json")
    with open(sb_path) as f:
        sb_data = json.load(f)
    bloco = sb_data["blocos"][index - 1]
    img_path = os.path.join(sb_dir, bloco["img"])
    request.files["imagem"].save(img_path)
    corrigir_orientacao(img_path)
    return jsonify({"ok": True})

@app.route("/girar_cena", methods=["POST"])
@login_required
def girar_cena():
    sb_id = request.form.get("sb_id")
    index = int(request.form.get("index"))
    graus = int(request.form.get("graus", 90))
    sb_dir = os.path.join(STORYBOARD_FOLDER, sb_id)
    sb_path = os.path.join(sb_dir, "storyboard.json")
    try:
        with open(sb_path) as f:
            sb_data = json.load(f)
        bloco = sb_data["blocos"][index - 1]
        img_path = os.path.join(sb_dir, bloco["img"])
        img = Image.open(img_path)
        img = img.rotate(-graus, expand=True)
        img.save(img_path, quality=95)
        img.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"erro": str(e)}), 500

@app.route("/editar_texto_cena", methods=["POST"])
@login_required
def editar_texto_cena():
    sb_id = request.json.get("sb_id")
    index = int(request.json.get("index"))
    texto = request.json.get("texto", "").strip()
    sb_dir = os.path.join(STORYBOARD_FOLDER, sb_id)
    sb_path = os.path.join(sb_dir, "storyboard.json")
    with open(sb_path) as f:
        sb_data = json.load(f)
    sb_data["blocos"][index - 1]["texto"] = texto
    with open(sb_path, "w") as f:
        json.dump(sb_data, f)
    return jsonify({"ok": True})

@app.route("/finalizar_video", methods=["POST"])
@login_required
def finalizar_video_route():
    sb_id = request.form.get("sb_id")
    voice_id = request.form.get("voice_id", "").strip()
    modo_video = request.form.get("modo_video", "longo")
    intervalo = int(request.form.get("intervalo", 2))
    legenda_cfg = {
        "ativo": request.form.get("legenda_ativo", "false") == "true",
        "fonte": request.form.get("legenda_fonte", "Arial"),
        "cor": request.form.get("legenda_cor", "&H00FFFFFF"),
        "tamanho": request.form.get("legenda_tamanho", "18"),
        "posicao": request.form.get("legenda_posicao", "2"),
        "sombra": request.form.get("legenda_sombra", "true") == "true",
    }
    animar_ia = request.form.get("animar_ia", "false") == "true"
    import sys
    sys.stderr.write(f"[ROTA] animar_ia={animar_ia}, form_value={request.form.get('animar_ia', 'NAO_ENVIADO')}\n")
    sys.stderr.flush()
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "aguardando", "progresso": "Na fila...", "total": 0, "atual": 0}
    thread = threading.Thread(target=finalizar_video, args=(job_id, current_user.id, sb_id, voice_id, modo_video, legenda_cfg, intervalo, animar_ia))
    thread.daemon = True
    thread.start()
    return jsonify({"job_id": job_id})

@app.route("/status/<job_id>")
@login_required
def status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"erro": "Job nao encontrado"}), 404
    return jsonify(job)

@app.route("/download/<job_id>")
@login_required
def download(job_id):
    criacao = Criacao.query.filter_by(job_id=job_id, user_id=current_user.id).first()
    job = jobs.get(job_id)
    zip_path = criacao.zip_path if criacao else (job.get("zip") if job else None)
    if not zip_path or not os.path.exists(zip_path):
        return jsonify({"erro": "Arquivo nao disponivel"}), 404
    return send_file(zip_path, as_attachment=True, download_name="imagens_geradas.zip")

@app.route("/download_video/<job_id>")
@login_required
def download_video(job_id):
    criacao = Criacao.query.filter_by(job_id=job_id, user_id=current_user.id).first()
    job = jobs.get(job_id)
    video_path = criacao.video_path if criacao else (job.get("video") if job else None)
    if not video_path or not os.path.exists(video_path):
        return jsonify({"erro": "Video nao disponivel"}), 404
    return send_file(video_path, as_attachment=True, download_name="video_gerado.mp4")

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(host="0.0.0.0", port=5000)
