from flask import Flask, request, render_template, send_file, jsonify, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import whisper, math, os, uuid, threading, zipfile, requests, subprocess, json, re, shutil, sqlite3, stripe
from pydub import AudioSegment
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from PIL import Image, ExifTags
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

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

# ── Chaves padrão do sistema (usadas quando usuário não tem API própria) ──
SYSTEM_OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
SYSTEM_MINIMAX_KEY = os.environ.get("MINIMAX_API_KEY", "")
SYSTEM_MINIMAX_GROUP_ID = os.environ.get("MINIMAX_GROUP_ID", "")

# ── Email Config ──
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
EMAIL_FROM = os.environ.get("EMAIL_FROM", SMTP_USER)

def enviar_email(destinatario, assunto, corpo_html):
    """Envia email em background"""
    if not SMTP_USER or not SMTP_PASS:
        return
    def _enviar():
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = assunto
            msg["From"] = f"Kaelum Studio <{EMAIL_FROM}>"
            msg["To"] = destinatario
            msg.attach(MIMEText(corpo_html, "html"))
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.starttls()
                server.login(SMTP_USER, SMTP_PASS)
                server.sendmail(EMAIL_FROM, destinatario, msg.as_string())
        except Exception as e:
            import sys
            sys.stderr.write(f"[EMAIL] Erro: {e}\n"); sys.stderr.flush()
    threading.Thread(target=_enviar, daemon=True).start()

CREDITOS_POR_IMAGEM = 12  # Base: gerar imagem
CREDITOS_MELHORAR_PROMPT = 3  # Melhorar prompt com IA
CREDITOS_NARRACAO = 5  # Narração por cena
CREDITOS_ANIMACAO = 60  # Animar cena com MiniMax Video
CREDITOS_CENA_COMPLETA = 80  # Tudo junto (12+3+5+60)

# Add-on Banco de Imagens
BANCO_ADDON_PRICE_ID = "price_1TPFcuLW3ZSF3MIlECdLofvd"
BANCO_ADDON_VALOR = "R$14,97/mês"

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
PRICE_MAP[BANCO_ADDON_PRICE_ID] = {"nome": "Banco de Imagens", "key": "banco", "tipo": "addon", "creditos": 0}

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
    "melhorar": """You are a cinematic storyboard artist and prompt engineer.
Goal: Convert narration text into a HIGH-QUALITY image prompt with VARIED compositions.

STRICT RULES:
1. STYLE FIRST: Start with: "{estilo}" of...". Mandatory.
2. COMPOSITION VARIETY: You MUST vary the shot type for each scene. Use these in rotation:
   - WIDE SHOT / PANORAMIC: For armies, landscapes, epic moments, establishing shots
   - MEDIUM SHOT: For dialogue, interactions between 2-3 characters
   - CLOSE-UP: For emotional moments, reactions (use sparingly, max 2 per story)
   - LOW ANGLE: For powerful/intimidating subjects (armies, warriors)
   - HIGH ANGLE / BIRD'S EYE: For showing scale, surrounded situations
   - OVER-THE-SHOULDER: For conversations
3. EPIC SCENES MUST BE EPIC: If the text describes armies, fire, supernatural events, or large-scale action, use WIDE PANORAMIC shots showing the FULL scale. Show hundreds of soldiers, massive flames filling the sky, mountains covered in fire. Do NOT reduce epic scenes to close-ups.
4. SCENE ACCURACY: Describe EXACTLY what the text says. Include ALL elements.
5. HISTORICAL ACCURACY: For biblical/ancient themes use ancient Middle Eastern attire, rough linen textures, desert sun.
6. SAFE DESCRIPTIONS: Instead of "defeated" say "lying on the ground exhausted". A giant = "extremely tall muscular man".
7. NO TEXT IN IMAGE: End every prompt with "no text, no letters, no words, no writing, no watermarks, no inscriptions".
8. AVOID REPETITION: Each image MUST look visually DIFFERENT from the previous one. Different angle, different framing, different lighting.
9. Output ONLY the prompt. Max 500 characters.""",
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
    banco_ativo = db.Column(db.Boolean, default=False)
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

    def get_api_key(self):
        """Retorna chave OpenAI do usuário ou do sistema"""
        if self.plano == "api_propria" and self.api_key:
            return self.api_key
        return self.api_key if self.api_key else SYSTEM_OPENAI_KEY

    def get_provider(self):
        """Retorna provider do usuário ou padrão"""
        if self.provider:
            return self.provider
        return "openai" if SYSTEM_OPENAI_KEY else ""

    def get_minimax_key(self):
        """Retorna chave MiniMax do usuário ou do sistema"""
        return self.minimax_key if self.minimax_key else SYSTEM_MINIMAX_KEY

    def get_minimax_group_id(self):
        """Retorna Group ID do usuário ou do sistema"""
        return self.minimax_group_id if self.minimax_group_id else SYSTEM_MINIMAX_GROUP_ID


def calcular_creditos_cena(melhorar_prompt=False, narracao=False, animar=False):
    """Calcula créditos por cena baseado nas features usadas"""
    total = CREDITOS_POR_IMAGEM
    if melhorar_prompt:
        total += CREDITOS_MELHORAR_PROMPT
    if narracao:
        total += CREDITOS_NARRACAO
    if animar:
        total += CREDITOS_ANIMACAO
    return total


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


MUSICAS_FOLDER = "musicas"
os.makedirs(MUSICAS_FOLDER, exist_ok=True)


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

def gerar_descricao_banco(prompt, tipo="imagem"):
    """Gera descrição e tags com IA pra facilitar busca no banco"""
    try:
        # Usa a chave do admin ou primeira chave disponível
        conn = sqlite3.connect('instance/veo3.db')
        row = conn.execute("SELECT api_key FROM user WHERE api_key != '' AND provider='openai' LIMIT 1").fetchone()
        conn.close()
        if not row:
            return prompt.lower(), prompt
        api_key = row[0]
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        tipo_label = "imagem" if tipo == "imagem" else "vídeo animado"
        body = {"model": "gpt-4o-mini", "messages": [
            {"role": "system", "content": f"Você é um catalogador de banco de {tipo_label}s. Dado o prompt usado pra gerar, crie:\n1. Uma descrição curta em português (max 100 chars)\n2. Tags de busca separadas por vírgula (palavras-chave em português)\n\nFormato:\nDESCRICAO: ...\nTAGS: ..."},
            {"role": "user", "content": prompt}
        ], "max_tokens": 150}
        r = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=body, timeout=15)
        if r.ok:
            resultado = r.json()["choices"][0]["message"]["content"].strip()
            descricao = ""
            tags = prompt.lower()
            for linha in resultado.split("\n"):
                if linha.upper().startswith("DESCRICAO:") or linha.upper().startswith("DESCRIÇÃO:"):
                    descricao = linha.split(":", 1)[1].strip()
                elif linha.upper().startswith("TAGS:"):
                    tags = linha.split(":", 1)[1].strip().lower()
            return tags, descricao or prompt[:100]
    except: pass
    return prompt.lower(), prompt[:100]

def salvar_no_banco(prompt, estilo, img_path, tipo="imagem", categoria=""):
    """Salva imagem ou vídeo no banco com metadados gerados por IA"""
    try:
        ext = img_path.rsplit(".", 1)[-1].lower() if "." in img_path else "png"
        nome = f"{uuid.uuid4().hex[:12]}.{ext}"
        destino = os.path.join(BANCO_IMG_FOLDER, nome)
        shutil.copy(img_path, destino)

        # Gerar descrição e tags com IA
        tags, descricao = gerar_descricao_banco(prompt, tipo)

        conn = sqlite3.connect('instance/veo3.db')
        try: conn.execute("ALTER TABLE banco_imagens ADD COLUMN tipo TEXT DEFAULT 'imagem'")
        except: pass
        try: conn.execute("ALTER TABLE banco_imagens ADD COLUMN categoria TEXT DEFAULT ''")
        except: pass
        try: conn.execute("ALTER TABLE banco_imagens ADD COLUMN descricao TEXT DEFAULT ''")
        except: pass
        conn.execute("INSERT INTO banco_imagens (prompt, estilo, tags, path, tipo, categoria, descricao) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (prompt, estilo, tags, destino, tipo, categoria, descricao))
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

def melhorar_prompt(texto, estilo, api_key, contexto_roteiro="", ficha_personagens="", direcao_criativa=""):
    prompts = load_prompts()
    estilo_det = ESTILOS_DETALHADOS.get(estilo, estilo) if estilo else "photorealistic, natural lighting, high quality, vertical composition"
    try:
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        system = prompts.get("melhorar", DEFAULT_PROMPTS["melhorar"]).replace("{estilo}", estilo_det)

        # Direção criativa do usuário
        if direcao_criativa:
            system += f"""

CREATIVE DIRECTION FROM THE USER:
"{direcao_criativa}"
You MUST incorporate this creative direction into every image prompt. It defines the mood, atmosphere, and visual elements the user wants."""
        if ficha_personagens:
            system += f"""

CHARACTER REFERENCE (use ONLY when the character appears in this scene):
{ficha_personagens}

Full story: "{contexto_roteiro}"
Current scene to illustrate: "{texto}"

CRITICAL RULES:
1. THE SCENE IS THE PRIORITY. Illustrate what the scene DESCRIBES, not just the main character.
2. If the scene describes an ARMY, show the ARMY as the main subject (wide shot, hundreds of soldiers). The character can be small in the frame or not visible.
3. If the scene describes FIRE, HORSES OF FIRE, supernatural events — make them the DOMINANT element. Fill 80% of the image with the spectacular element.
4. Only show a character in close-up if the scene is specifically about their EMOTION or DIALOGUE.
5. VARY the main subject: sometimes the landscape, sometimes the army, sometimes the character, sometimes the supernatural event.
6. When characters appear, use their description from the reference above to keep them consistent.
7. NEVER put the same character as the main close-up subject in more than 2 consecutive scenes.
8. For scenes with armies/battles: use WIDE PANORAMIC shots, bird's eye view, or dramatic low angles showing scale."""
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

def extrair_personagens(roteiro, api_key, estilo=""):
    """Extrai ficha de personagens ultra-detalhada pra consistência visual"""
    estilo_det = ESTILOS_DETALHADOS.get(estilo, estilo) if estilo else "default style"
    try:
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        system = f"""You are a character designer creating a STRICT visual reference sheet for an AI image generator.
The art style is: {estilo_det}
Given a story, create an EXTREMELY detailed and FIXED description for each character and important object.

OUTPUT FORMAT (one per line):
CHARACTER_NAME: [complete visual description]
ART_STYLE: [exact art style rules for ALL images]
BACKGROUND: [consistent background for ALL scenes]

CRITICAL RULES:
1. ART STYLE CONSISTENCY: Define the EXACT art style once (line thickness, color palette, shading type, eye style) and it MUST be identical in every scene. For cartoon: specify "thick black outlines, flat colors, simple shapes". For anime: specify "cel shading, large expressive eyes, thin lines".
2. FACE: Describe exact eye shape, eye SIZE (small/medium/large), eye COLOR, eyebrow shape, nose shape, mouth expression. For cartoon/anime: describe the EXACT eye style (dot eyes, round eyes, anime eyes with highlights, etc.)
3. HAIR: Exact color (use hex if possible), length, style
4. CLOTHING: Exact colors with specific names (e.g. "bright orange #FF8C00 t-shirt"), exact garment types. These MUST NOT change between scenes.
5. SKIN: Exact skin tone that stays consistent
6. BACKGROUND: Define ONE consistent background color or setting for ALL scenes (e.g. "solid light blue #87CEEB background" or "solid yellow #FFD700 background")
7. ANATOMY: "peito do pé" = "top of the foot / instep" (NOT sole). "sola do pé" = "sole / bottom of the foot"
8. Write in ENGLISH
9. Be so specific that the same character looks IDENTICAL in every single frame"""
        body = {"model": "gpt-4o-mini", "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": roteiro}
        ], "max_tokens": 600}
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
        raise Exception("Estamos com problemas técnicos na narração. Por favor, tente novamente mais tarde. Se o erro persistir, entre em contato com o suporte técnico.")
    data = r.json()
    if data.get("base_resp", {}).get("status_code") != 0:
        raise Exception("Estamos com problemas técnicos na narração. Por favor, tente novamente mais tarde. Se o erro persistir, entre em contato com o suporte técnico.")
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
        raise Exception("Estamos com problemas técnicos na clonagem de voz. Por favor, tente novamente mais tarde.")
    file_id = r.json().get("file", {}).get("file_id")
    if not file_id:
        raise Exception("Estamos com problemas técnicos na clonagem de voz. Por favor, tente novamente mais tarde.")
    url_clone = f"https://api.minimaxi.chat/v1/voice_clone?GroupId={group_id}"
    r2 = requests.post(url_clone, headers={**headers, "Content-Type": "application/json"},
                       json={"file_id": int(file_id), "voice_id": voice_id, "need_noise_reduction": True, "need_volume_normalization": True}, timeout=120)
    if not r2.ok:
        raise Exception("Estamos com problemas técnicos na clonagem de voz. Por favor, tente novamente mais tarde.")
    result = r2.json()
    if result.get("base_resp", {}).get("status_code") != 0:
        raise Exception("Estamos com problemas técnicos na clonagem de voz. Por favor, tente novamente mais tarde.")
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
    import base64, time, sys
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    # Converter imagem pra base64
    with open(img_path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode()
    ext = img_path.rsplit(".", 1)[-1].lower()
    mime = "image/png" if ext == "png" else "image/jpeg"
    img_data_url = f"data:{mime};base64,{img_b64}"

    # Criar task com modelo rápido
    body = {
        "model": "MiniMax-Hailuo-2.3-Fast",
        "prompt": prompt,
        "first_frame_image": img_data_url,
        "duration": 6,
        "resolution": "768P",
    }
    t0 = time.time()
    r = requests.post("https://api.minimax.io/v1/video_generation", headers=headers, json=body, timeout=60)
    if not r.ok:
        if r.status_code == 429 or "rate" in r.text.lower():
            raise Exception("RATE_LIMIT:Estamos com uma alta demanda no momento. Por favor, tente novamente mais tarde.")
        raise Exception("Estamos com problemas técnicos na animação. Por favor, tente novamente mais tarde.")
    data = r.json()
    task_id = data.get("task_id")
    if not task_id:
        resp_str = str(data)
        if "1002" in resp_str or "1008" in resp_str or "rate" in resp_str.lower() or "insufficient" in resp_str.lower():
            raise Exception("RATE_LIMIT:Estamos com uma alta demanda no momento. Por favor, tente novamente mais tarde.")
        raise Exception("Estamos com problemas técnicos na animação. Por favor, tente novamente mais tarde.")
    sys.stderr.write(f"[VIDEO] Task criada: {task_id}\n"); sys.stderr.flush()

    # Poll status (a cada 5s em vez de 10s)
    for _ in range(180):  # max 15 min
        time.sleep(5)
        try:
            r2 = requests.get("https://api.minimax.io/v1/query/video_generation",
                              headers=headers, params={"task_id": task_id}, timeout=15)
            if not r2.ok:
                continue
            status_data = r2.json()
            status = status_data.get("status", "")
            if status == "Success":
                file_id = status_data.get("file_id")
                if not file_id:
                    raise Exception("Estamos com problemas técnicos na animação. Por favor, tente novamente mais tarde.")
                r3 = requests.get("https://api.minimax.io/v1/files/retrieve",
                                  headers=headers, params={"file_id": file_id}, timeout=15)
                if not r3.ok:
                    raise Exception("Estamos com problemas técnicos na animação. Por favor, tente novamente mais tarde.")
                download_url = r3.json().get("file", {}).get("download_url")
                if not download_url:
                    raise Exception("Estamos com problemas técnicos na animação. Por favor, tente novamente mais tarde.")
                video_r = requests.get(download_url, timeout=120)
                with open(output_path, "wb") as f:
                    f.write(video_r.content)
                dt = time.time() - t0
                sys.stderr.write(f"[VIDEO] OK em {dt:.0f}s\n"); sys.stderr.flush()
                return True
            elif status == "Fail":
                raise Exception("Estamos com problemas técnicos na animação. Por favor, tente novamente mais tarde.")
        except requests.exceptions.Timeout:
            continue
    raise Exception("Estamos com problemas técnicos na animação. Tempo limite excedido. Por favor, tente novamente mais tarde.")

def gerar_imagem_openai(prompt, api_key, size, quality, output_path, modelo="dall-e-3"):
    import sys, time as _time
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    if modelo == "gpt-image-1":
        import base64
        size_map = {"1024x1024": "1024x1024", "1792x1024": "1536x1024", "1024x1792": "1024x1536"}
        gpt_size = size_map.get(size, "1024x1536")
        body = {"model": "gpt-image-1", "prompt": prompt, "n": 1, "size": gpt_size, "quality": "medium", "output_format": "png"}
        t0 = _time.time()
        sys.stderr.write(f"[IMG] gpt-image-1 iniciando...\n"); sys.stderr.flush()
        r = requests.post("https://api.openai.com/v1/images/generations", headers=headers, json=body, timeout=120)
        dt = _time.time() - t0
        if r.ok:
            data = r.json()
            img_bytes = base64.b64decode(data["data"][0]["b64_json"])
            with open(output_path, "wb") as f:
                f.write(img_bytes)
            sys.stderr.write(f"[IMG] gpt-image-1 OK em {dt:.1f}s\n"); sys.stderr.flush()
            return
        erro = r.json().get("error", {}).get("message", "")
        sys.stderr.write(f"[IMG] gpt-image-1 erro em {dt:.1f}s: {erro}\n"); sys.stderr.flush()
        if "model" in erro.lower() or "access" in erro.lower() or "permission" in erro.lower():
            modelo = "dall-e-3"
        elif "safety" in erro.lower() or "rejected" in erro.lower():
            # Suavizar e tentar de novo com gpt-image-1
            prompt = suavizar_prompt(prompt, api_key)
            body["prompt"] = prompt
            r2 = requests.post("https://api.openai.com/v1/images/generations", headers=headers, json=body, timeout=120)
            if r2.ok:
                data = r2.json()
                img_bytes = base64.b64decode(data["data"][0]["b64_json"])
                with open(output_path, "wb") as f:
                    f.write(img_bytes)
                sys.stderr.write(f"[IMG] gpt-image-1 OK (suavizado)\n"); sys.stderr.flush()
                return
            # Se falhar de novo, tenta dall-e-3
            modelo = "dall-e-3"
        else:
            raise Exception("Estamos com problemas técnicos na geração de imagens. Por favor, tente novamente mais tarde. Se o erro persistir, entre em contato com o suporte técnico.")

    # DALL-E 3
    tamanhos_validos = ["1024x1024", "1792x1024", "1024x1792"]
    if size not in tamanhos_validos:
        size = "1024x1024"
    for tentativa in range(3):
        body = {"model": "dall-e-3", "prompt": prompt, "n": 1, "size": size, "quality": quality, "response_format": "url"}
        t0 = _time.time()
        r = requests.post("https://api.openai.com/v1/images/generations", headers=headers, json=body, timeout=60)
        dt = _time.time() - t0
        if r.ok:
            sys.stderr.write(f"[IMG] dall-e-3 OK em {dt:.1f}s\n"); sys.stderr.flush()
            img_r = requests.get(r.json()["data"][0]["url"], timeout=60)
            with open(output_path, "wb") as f:
                f.write(img_r.content)
            corrigir_orientacao(output_path)
            return
        erro = r.json().get("error", {}).get("message", "")
        sys.stderr.write(f"[IMG] dall-e-3 erro tentativa {tentativa+1}: {erro}\n"); sys.stderr.flush()
        if ("safety" in erro.lower() or "content" in erro.lower()) and tentativa < 2:
            prompt = suavizar_prompt(prompt, api_key)
            continue
        raise Exception("Estamos com problemas técnicos na geração de imagens. Por favor, tente novamente mais tarde. Se o erro persistir, entre em contato com o suporte técnico.")

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
            raise Exception("Estamos com problemas técnicos na geração de imagens. Por favor, tente novamente mais tarde.")
    raise Exception("Estamos com problemas técnicos na geração de imagens. Tempo limite excedido. Por favor, tente novamente mais tarde.")

def gerar_imagem(prompt, user, output_path, estilo="", usar_banco=False, formato="vertical"):
    provider = user.get_provider()
    api_key = user.get_api_key()
    if not api_key:
        raise Exception("Nenhuma chave de API disponível. Entre em contato com o suporte.")
    # Mapear formato pra tamanho
    formato_map = {"vertical": "1024x1792", "horizontal": "1792x1024", "quadrado": "1024x1024"}
    size = formato_map.get(formato, user.image_size or "1024x1792")
    if provider == "openai":
        gerar_imagem_openai(prompt, api_key, size, user.quality or "standard", output_path, modelo="gpt-image-1")
    elif provider == "replicate":
        gerar_imagem_replicate(prompt, api_key, output_path)
    else:
        raise Exception("Nenhuma chave de API disponível. Entre em contato com o suporte.")
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

    # Suportar vídeos de até 3 min (~36 cenas de 5s)
    max_cenas = max(2, min(36, len(texto) // 30))

    prompts = load_prompts()
    try:
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        system = prompts.get("dividir", DEFAULT_PROMPTS["dividir"])
        system += f"\n\nIMPORTANT: Create a MAXIMUM of {max_cenas} scenes. Each scene must be a meaningful story beat. You MUST include ALL parts of the text from beginning to end - do NOT skip, cut, or omit ANY part. The ENTIRE text must be covered."
        body = {"model": "gpt-4o-mini", "messages": [
            {"role": "system", "content": system}, {"role": "user", "content": texto}
        ], "max_tokens": 4000}
        r = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=body, timeout=60)
        if r.ok:
            resultado = r.json()["choices"][0]["message"]["content"].strip()
            linhas = [l.strip() for l in resultado.split("\n") if l.strip()]
            if len(linhas) > max_cenas:
                linhas = linhas[:max_cenas]
            if len(linhas) >= 1:
                return linhas
    except: pass
    return [l.strip() for l in texto.replace(",", "\n").replace(".", "\n").split("\n") if l.strip()] or [texto.strip()]

def gerar_storyboard(job_id, user_id, texto_manual, estilo, melhorar_prompts, usar_banco=False, cenas_preenchidas=None, direcao_criativa="", formato="vertical"):
    if cenas_preenchidas is None:
        cenas_preenchidas = {}
    with app.app_context():
        try:
            resetar_banco_usadas()
            user = User.query.get(user_id)
            jobs[job_id] = {"status": "processando", "progresso": "Analisando roteiro...", "total": 0, "atual": 0}
            sb_dir = os.path.join(STORYBOARD_FOLDER, job_id)
            os.makedirs(sb_dir, exist_ok=True)
            if user.get_provider() == "openai":
                linhas = dividir_roteiro(texto_manual, user.get_api_key())
            else:
                linhas = [l.strip() for l in texto_manual.replace(",", "\n").replace(".", "\n").split("\n") if l.strip()]

            total = len(linhas)
            # Contar quantas cenas precisam ser geradas (excluir preenchidas)
            cenas_a_gerar = [i for i in range(total) if str(i+1) not in cenas_preenchidas]
            creditos_por_cena = calcular_creditos_cena(melhorar_prompt=melhorar_prompts, narracao=False, animar=False)
            creditos_necessarios = len(cenas_a_gerar) * creditos_por_cena
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
            if melhorar_prompts and user.get_provider() == "openai" and cenas_a_gerar:
                jobs[job_id]["progresso"] = "Analisando personagens..."
                ficha = extrair_personagens(texto_manual, user.get_api_key(), estilo)

            def gerar_bloco(i_linha):
                linha = linhas[i_linha]
                if melhorar_prompts and user.get_provider() == "openai":
                    prompt_final = melhorar_prompt(linha, estilo, user.get_api_key(), roteiro_completo, ficha, direcao_criativa)
                else:
                    prompt_final = f"{linha}, {estilo}" if estilo else linha
                img_path = os.path.join(sb_dir, f"{i_linha+1:03d}.png")
                gerar_imagem(prompt_final, user, img_path, estilo, formato=formato)
                blocos.append({"index": i_linha+1, "texto": linha, "img": f"{i_linha+1:03d}.png"})
                jobs[job_id]["atual"] = len(blocos)
                jobs[job_id]["progresso"] = f"Gerando imagem {len(blocos)} de {total}..."

            if cenas_a_gerar:
                with ThreadPoolExecutor(max_workers=5) as executor:
                    list(executor.map(gerar_bloco, cenas_a_gerar))

            # Gastar créditos apenas pelas cenas geradas
            if cenas_a_gerar:
                creditos_por_cena = calcular_creditos_cena(melhorar_prompt=melhorar_prompts, narracao=False, animar=False)
                user.gastar_creditos(len(cenas_a_gerar) * creditos_por_cena)
                db.session.commit()

            blocos.sort(key=lambda x: x["index"])
            sb_data = {"blocos": blocos, "estilo": estilo, "dir": sb_dir}
            with open(os.path.join(sb_dir, "storyboard.json"), "w") as f:
                json.dump(sb_data, f)
            jobs[job_id] = {"status": "storyboard_pronto", "progresso": "Storyboard pronto", "total": total, "atual": total, "blocos": blocos, "sb_id": job_id}
        except Exception as e:
            jobs[job_id] = {"status": "erro", "progresso": str(e), "total": 0, "atual": 0}

def finalizar_video(job_id, user_id, sb_id, voice_id, modo_video, legenda_cfg, intervalo, animar_ia=False, musica_path=""):
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

            if user.get_minimax_key() and voice_id:
                # Cobrar créditos de narração
                creditos_narracao = len(blocos) * CREDITOS_NARRACAO
                if not user.gastar_creditos(creditos_narracao):
                    jobs[job_id] = {"status": "erro", "progresso": f"Créditos insuficientes para narração. Necessário: {creditos_narracao}", "total": 0, "atual": 0}
                    return
                db.session.commit()
                jobs[job_id]["progresso"] = "Gerando narração completa..."
                audios = []
                imagens = []
                t = 0
                import time as _time

                # Narrar texto completo de uma vez (evita rate limit)
                texto_completo = ". ".join([b["texto"] for b in blocos])
                audio_completo_path = os.path.join(job_dir, "narracao.mp3")
                narracao_ok = False
                for tentativa in range(3):
                    try:
                        gerar_audio_minimax(texto_completo, user.get_minimax_key(), user.get_minimax_group_id(), voice_id, audio_completo_path)
                        narracao_ok = True
                        break
                    except Exception as e:
                        if "1002" in str(e) or "rate" in str(e).lower():
                            _time.sleep(15)
                            continue
                        raise
                if not narracao_ok:
                    jobs[job_id] = {"status": "erro", "progresso": "Estamos com uma alta demanda no momento. Por favor, tente novamente mais tarde.", "total": 0, "atual": 0}
                    return

                # Transcrever pra pegar timestamps de cada frase
                jobs[job_id]["progresso"] = "Sincronizando narração com cenas..."
                audio_completo_seg = AudioSegment.from_file(audio_completo_path)
                duracao_total = len(audio_completo_seg) / 1000

                if modo_video == "shorts":
                    audio_limpo_path = os.path.join(job_dir, "narracao_limpa.mp3")
                    remover_silencio(audio_completo_path, audio_limpo_path)
                    audio_completo_seg = AudioSegment.from_file(audio_limpo_path)
                    duracao_total = len(audio_completo_seg) / 1000
                    audio_completo_path = audio_limpo_path

                # Dividir duração proporcionalmente entre cenas
                dur_por_cena = duracao_total / len(blocos)
                for i, bloco in enumerate(blocos):
                    img_src = os.path.join(sb_dir, bloco["img"])
                    img_dst = os.path.join(job_dir, f"{i+1:04d}.png")
                    shutil.copy(img_src, img_dst)
                    imagens.append({"index": i+1, "path": img_dst, "duracao": round(dur_por_cena, 2),
                                    "inicio": round(t, 2), "fim": round(t + dur_por_cena, 2),
                                    "texto": bloco["texto"]})
                    t += dur_por_cena

                audio_final_path = audio_completo_path
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
            sys.stderr.write(f"[VIDEO] animar_ia={animar_ia}, minimax_key={'SIM' if user.get_minimax_key() else 'NAO'}\n")
            sys.stderr.flush()
            if animar_ia and user.get_minimax_key():
                # Básico não tem animação
                if user.plano == "basico":
                    import sys
                    sys.stderr.write("[VIDEO] Plano basico nao tem animacao, pulando\n")
                    animar_ia = False

            if animar_ia and user.get_minimax_key():
                n_cenas = len(imagens)
                minimax_key_cache = user.get_minimax_key()
                tempo_est = max(5, (n_cenas // 3 + 1) * 2)
                jobs[job_id]["progresso"] = f"Animando {n_cenas} cenas com IA. Tempo estimado: ~{tempo_est} minutos..."
                clipes_video = [None] * n_cenas

                def animar_cena(i):
                    img = imagens[i]
                    clipe_path = os.path.join(job_dir, f"clipe_{i+1:04d}.mp4")

                    # Verificar se a cena já tem vídeo do banco
                    bloco = blocos[i] if i < len(blocos) else {}
                    if bloco.get("video"):
                        video_banco = os.path.join(sb_dir, bloco["video"])
                        if os.path.exists(video_banco):
                            shutil.copy(video_banco, clipe_path)
                            clipes_video[i] = clipe_path
                            jobs[job_id]["atual"] = sum(1 for c in clipes_video if c is not None)
                            jobs[job_id]["progresso"] = f"Animando cenas... {jobs[job_id]['atual']}/{n_cenas} prontas (~{tempo_est} min)"
                            sys.stderr.write(f"[ANIMAR] Cena {i+1}: usando video do banco\n"); sys.stderr.flush()
                            return

                    import time as _t
                    for tentativa in range(3):
                        try:
                            # Melhorar prompt pra animação dinâmica
                            anim_prompt = f"[Tracking shot] {img['texto']}. Dynamic motion, cinematic camera movement, characters moving naturally, expressive body language, fluid animation."
                            gerar_video_minimax(img["path"], anim_prompt, minimax_key_cache, clipe_path)
                            clipes_video[i] = clipe_path
                            # Salvar no banco imediatamente (path absoluto)
                            try:
                                import sqlite3 as _sql3
                                _nome = f"{uuid.uuid4().hex[:12]}.mp4"
                                _destino = os.path.join(os.path.abspath(BANCO_IMG_FOLDER), _nome)
                                shutil.copy(clipe_path, _destino)
                                _conn = _sql3.connect(os.path.join(os.path.abspath('instance'), 'veo3.db'))
                                _conn.execute("INSERT INTO banco_imagens (prompt, estilo, tags, path, tipo, categoria, descricao) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                    (img["texto"], "", img["texto"].lower(), _destino, "video", "cena_animada", img["texto"][:100]))
                                _conn.commit()
                                _conn.close()
                                sys.stderr.write(f"[ANIMAR] Cena {i+1}: salva no banco\n"); sys.stderr.flush()
                            except Exception as be:
                                sys.stderr.write(f"[ANIMAR] Cena {i+1}: erro ao salvar no banco: {be}\n"); sys.stderr.flush()
                            jobs[job_id]["atual"] = sum(1 for c in clipes_video if c is not None)
                            jobs[job_id]["progresso"] = f"Animando cenas... {jobs[job_id]['atual']}/{n_cenas} prontas (~{tempo_est} min)"
                            return
                        except Exception as e:
                            erro_str = str(e)
                            import traceback
                            sys.stderr.write(f"[ANIMAR] Cena {i+1} tentativa {tentativa+1}: {erro_str}\n{traceback.format_exc()}\n"); sys.stderr.flush()
                            if "1002" in erro_str or "rate" in erro_str.lower():
                                _t.sleep(30)
                                continue
                            clipes_video[i] = None
                            return
                    clipes_video[i] = None

                # Animar em paralelo (3 por vez) com retry
                import time as _time
                lote_size = 3
                for lote_start in range(0, n_cenas, lote_size):
                    lote_end = min(lote_start + lote_size, n_cenas)
                    lote = list(range(lote_start, lote_end))
                    with ThreadPoolExecutor(max_workers=lote_size) as executor:
                        list(executor.map(animar_cena, lote))
                    if lote_end < n_cenas:
                        _time.sleep(2)

                # Cobrar créditos só pelas animações que deram certo
                cenas_animadas = sum(1 for c in clipes_video if c is not None)
                if cenas_animadas > 0:
                    user.gastar_creditos(cenas_animadas * CREDITOS_ANIMACAO)
                    db.session.commit()

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
                    # Nenhum clipe gerado — verificar se foi rate limit ou erro técnico
                    jobs[job_id] = {"status": "erro", "progresso": "Estamos com uma alta demanda no momento. Por favor, tente novamente mais tarde. Se o erro persistir, entre em contato com o suporte técnico.", "total": 0, "atual": 0}
                    return
            else:
                # Verificar se alguma cena tem vídeo do banco
                tem_video_banco = any(b.get("video") for b in blocos)
                if tem_video_banco:
                    # Montar com mix de imagens e vídeos
                    jobs[job_id]["progresso"] = "Montando video com cenas mistas..."
                    video_path = os.path.join(OUTPUT_FOLDER, f"{job_id}.mp4")
                    concat_path = os.path.join(job_dir, "concat_mix.txt")
                    clipes_temp = []
                    for i, img in enumerate(imagens):
                        bloco = blocos[i] if i < len(blocos) else {}
                        if bloco.get("video"):
                            video_banco = os.path.join(sb_dir, bloco["video"])
                            if os.path.exists(video_banco):
                                clipes_temp.append(video_banco)
                                continue
                        # Imagem estática: criar clipe com zoompan
                        clipe_img = os.path.join(job_dir, f"clip_img_{i:04d}.mp4")
                        dur = img["duracao"]
                        cmd_img = ["ffmpeg", "-y", "-loop", "1", "-t", str(dur+1), "-i", os.path.abspath(img["path"]),
                                   "-vf", f"scale=1024:1792,zoompan=z='min(zoom+0.0008,1.12)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={int(dur*25)}:s=1024x1792:fps=25,trim=duration={dur},setpts=PTS-STARTPTS",
                                   "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p", clipe_img]
                        subprocess.run(cmd_img, capture_output=True, text=True, timeout=60)
                        if os.path.exists(clipe_img):
                            clipes_temp.append(clipe_img)
                    # Concatenar
                    with open(concat_path, "w") as f:
                        for cp in clipes_temp:
                            f.write(f"file '{os.path.abspath(cp)}'\n")
                    cmd_concat = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_path]
                    if audio_final_path and os.path.exists(audio_final_path):
                        cmd_concat += ["-i", audio_final_path, "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest"]
                    else:
                        cmd_concat += ["-c:v", "copy"]
                    cmd_concat += [video_path]
                    subprocess.run(cmd_concat, capture_output=True, text=True, timeout=300)
                else:
                    jobs[job_id]["progresso"] = "Montando video..."
                    video_path = os.path.join(OUTPUT_FOLDER, f"{job_id}.mp4")
                    montar_video(imagens, audio_final_path, video_path, legenda_cfg)

            # Mixar música de fundo se selecionada
            if musica_path and os.path.exists(musica_path) and os.path.exists(video_path):
                jobs[job_id]["progresso"] = "Adicionando música de fundo..."
                video_com_musica = video_path.replace(".mp4", "_music.mp4")
                try:
                    # Mixar: narração em volume normal + música em volume baixo
                    if audio_final_path and os.path.exists(audio_final_path):
                        cmd = ["ffmpeg", "-y", "-i", video_path, "-i", musica_path,
                               "-filter_complex", "[1:a]volume=0.15[bg];[0:a][bg]amix=inputs=2:duration=first[aout]",
                               "-map", "0:v", "-map", "[aout]", "-c:v", "copy", "-c:a", "aac", "-shortest", video_com_musica]
                    else:
                        # Sem narração: música como áudio principal (volume normal)
                        cmd = ["ffmpeg", "-y", "-i", video_path, "-i", musica_path,
                               "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-shortest", video_com_musica]
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                    if result.returncode == 0 and os.path.exists(video_com_musica):
                        os.replace(video_com_musica, video_path)
                except Exception as e:
                    import sys
                    sys.stderr.write(f"[MUSICA] Erro ao mixar: {e}\n"); sys.stderr.flush()

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
    return render_template("landing.html")

@app.route("/termos")
def termos():
    return render_template("termos.html")

@app.route("/privacidade")
def privacidade():
    return render_template("privacidade.html")

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
        # Email de boas-vindas
        enviar_email(user.email, "Bem-vindo ao Kaelum Studio! 🎬", f"""
        <div style="font-family:Arial;max-width:500px;margin:0 auto;padding:20px">
            <h1 style="color:#4a9eff">Kaelum Studio</h1>
            <p>Olá <b>{user.nome}</b>! 👋</p>
            <p>Sua conta foi criada com sucesso. Agora você pode criar vídeos incríveis com inteligência artificial.</p>
            <p>Escolha um plano e comece a criar:</p>
            <a href="https://kaelumstudio.grupomafort.com/dashboard" style="display:inline-block;padding:12px 24px;background:#2563eb;color:#fff;border-radius:8px;text-decoration:none;font-weight:600">Acessar Kaelum Studio</a>
            <p style="color:#888;font-size:12px;margin-top:20px">Kaelum Studio — AI Video Automation</p>
        </div>""")
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
                    current_user.creditos = creditos
                elif tipo == "avulso":
                    current_user.creditos += creditos
                elif tipo == "addon":
                    if plano_key == "banco":
                        current_user.banco_ativo = True

                db.session.commit()
        except Exception as e:
            print(f"Erro ao processar pagamento: {e}")
    return redirect(url_for("dashboard") + "?pagamento=sucesso")

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
                elif tipo == "addon":
                    if plano_key == "banco":
                        user.banco_ativo = True
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
                           creditos_por_imagem=CREDITOS_POR_IMAGEM,
                           creditos_prompt=CREDITOS_MELHORAR_PROMPT,
                           creditos_narracao=CREDITOS_NARRACAO,
                           creditos_animacao=CREDITOS_ANIMACAO,
                           banco_addon_price=BANCO_ADDON_PRICE_ID,
                           banco_addon_valor=BANCO_ADDON_VALOR)

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

@app.route("/admin/toggle_banco", methods=["POST"])
@login_required
def admin_toggle_banco():
    if not current_user.is_admin:
        return jsonify({"erro": "Sem permissao"}), 403
    user_id = request.json.get("user_id")
    user = User.query.get(user_id)
    if user:
        user.banco_ativo = not user.banco_ativo
        db.session.commit()
        return jsonify({"ok": True, "banco_ativo": user.banco_ativo})
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

@app.route("/admin/verificar_apis")
@login_required
def admin_verificar_apis():
    if not current_user.is_admin:
        return jsonify({"erro": "Sem permissao"}), 403
    resultado = {}
    # Verificar OpenAI
    try:
        headers = {"Authorization": f"Bearer {SYSTEM_OPENAI_KEY}"}
        r = requests.get("https://api.openai.com/v1/models", headers=headers, timeout=10)
        if r.ok:
            resultado["openai"] = {"status": "ok", "msg": "API funcionando"}
        else:
            resultado["openai"] = {"status": "erro", "msg": r.json().get("error", {}).get("message", "Erro desconhecido")}
    except Exception as e:
        resultado["openai"] = {"status": "erro", "msg": str(e)}
    # Verificar MiniMax
    try:
        headers = {"Authorization": f"Bearer {SYSTEM_MINIMAX_KEY}"}
        r = requests.get("https://api.minimax.io/v1/query/video_generation", headers=headers, params={"task_id": "test"}, timeout=10)
        if r.status_code != 401:
            resultado["minimax"] = {"status": "ok", "msg": "API funcionando"}
        else:
            resultado["minimax"] = {"status": "erro", "msg": "Chave inválida"}
    except Exception as e:
        resultado["minimax"] = {"status": "erro", "msg": str(e)}
    # Verificar Stripe
    try:
        bal = stripe.Balance.retrieve()
        saldo_brl = sum(b.amount/100 for b in bal.available if b.currency == "brl")
        resultado["stripe"] = {"status": "ok", "msg": f"Saldo: R${saldo_brl:.2f}"}
    except Exception as e:
        resultado["stripe"] = {"status": "erro", "msg": str(e)}
    return jsonify(resultado)

BRANDING_FILE = "branding_config.json"

def load_branding():
    try:
        if os.path.exists(BRANDING_FILE):
            with open(BRANDING_FILE) as f:
                return json.load(f)
    except: pass
    return {"cor_primaria": "#1a2332", "cor_accent": "#4a9eff", "nome": "Kaelum Studio", "subtitulo": "AI Video Automation", "logo": "", "icone": ""}

def save_branding(data):
    with open(BRANDING_FILE, "w") as f:
        json.dump(data, f, indent=2)

@app.route("/admin/branding", methods=["GET", "POST"])
@login_required
def admin_branding():
    if not current_user.is_admin:
        return jsonify({"erro": "Sem permissao"}), 403
    if request.method == "POST":
        branding = load_branding()
        branding["cor_primaria"] = request.form.get("cor_primaria", branding["cor_primaria"])
        branding["cor_accent"] = request.form.get("cor_accent", branding["cor_accent"])
        branding["nome"] = request.form.get("nome", branding["nome"])
        branding["subtitulo"] = request.form.get("subtitulo", branding["subtitulo"])

        os.makedirs("static", exist_ok=True)
        if "logo" in request.files and request.files["logo"].filename:
            logo = request.files["logo"]
            logo_path = os.path.join("static", "logo.png")
            logo.save(logo_path)
            branding["logo"] = "/static/logo.png"
            # Enviar pra Stripe
            try:
                with open(logo_path, "rb") as f:
                    file_upload = stripe.File.create(purpose="business_logo", file=f)
                    # Não dá pra setar via API em conta própria, mas o arquivo fica disponível
            except: pass

        if "icone" in request.files and request.files["icone"].filename:
            icone = request.files["icone"]
            icone_path = os.path.join("static", "icone.png")
            icone.save(icone_path)
            branding["icone"] = "/static/icone.png"

        save_branding(branding)
        return jsonify({"ok": True})
    return jsonify(load_branding())

@app.route("/static/<path:filename>")
def static_files(filename):
    return send_file(os.path.join("static", filename))

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
    tipo_filtro = request.json.get("tipo", "")
    estilo = request.json.get("estilo", "")
    pagina = int(request.json.get("pagina", 1))
    por_pagina = 20
    offset = (pagina - 1) * por_pagina

    conn = sqlite3.connect('instance/veo3.db')
    query = "SELECT id, prompt, path, estilo, tipo, categoria, descricao FROM banco_imagens WHERE 1=1"
    count_query = "SELECT COUNT(*) FROM banco_imagens WHERE 1=1"
    params = []
    if termo:
        filtro = " AND (tags LIKE ? OR prompt LIKE ? OR descricao LIKE ?)"
        query += filtro
        count_query += filtro
        params += [f"%{termo}%", f"%{termo}%", f"%{termo}%"]
    if tipo_filtro:
        query += " AND tipo = ?"
        count_query += " AND tipo = ?"
        params.append(tipo_filtro)
    if estilo:
        query += " AND estilo = ?"
        count_query += " AND estilo = ?"
        params.append(estilo)

    try:
        total = conn.execute(count_query, params).fetchone()[0]
        query += f" ORDER BY id DESC LIMIT {por_pagina} OFFSET {offset}"
        rows = conn.execute(query, params).fetchall()
    except:
        total = 0
        rows = conn.execute("SELECT id, prompt, path, estilo, 'imagem', '', prompt FROM banco_imagens ORDER BY id DESC LIMIT 20").fetchall()
    conn.close()

    imgs = []
    for r in rows:
        if os.path.exists(r[2]):
            imgs.append({
                "id": r[0], "prompt": r[1], "path": os.path.basename(r[2]),
                "estilo": r[3] or "", "tipo": r[4] or "imagem",
                "categoria": r[5] or "", "descricao": r[6] or r[1]
            })
    tem_mais = (pagina * por_pagina) < total
    return jsonify({"imgs": imgs, "total": total, "pagina": pagina, "tem_mais": tem_mais})

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
    try:
        row = conn.execute("SELECT path, tipo FROM banco_imagens WHERE id=?", (img_id,)).fetchone()
    except:
        row = conn.execute("SELECT path FROM banco_imagens WHERE id=?", (img_id,)).fetchone()
        if row: row = (row[0], "imagem")
    conn.close()
    if row and os.path.exists(row[0]):
        src_path = row[0]
        tipo = row[1] if len(row) > 1 else "imagem"
        if tipo == "video" or src_path.endswith(".mp4"):
            # Salvar vídeo na pasta do storyboard
            video_name = f"{index:03d}.mp4"
            shutil.copy(src_path, os.path.join(sb_dir, video_name))
            bloco["video"] = video_name
            # Extrair thumbnail pra mostrar no storyboard
            thumb_path = os.path.join(sb_dir, bloco["img"])
            try:
                subprocess.run(["ffmpeg", "-y", "-i", src_path, "-vframes", "1", "-q:v", "2", thumb_path],
                               capture_output=True, text=True, timeout=10)
            except: pass
        else:
            shutil.copy(src_path, os.path.join(sb_dir, bloco["img"]))
            bloco.pop("video", None)
        with open(sb_path, "w") as f:
            json.dump(sb_data, f)
        return jsonify({"ok": True, "tipo": tipo})
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

# ── Rotas Música ─────────────────────────────────────────
@app.route("/upload_musica", methods=["POST"])
@login_required
def upload_musica():
    if "musica" not in request.files or not request.files["musica"].filename:
        return jsonify({"erro": "Envie um arquivo de música"}), 400
    musica = request.files["musica"]
    nome = request.form.get("nome", "").strip() or musica.filename
    if not musica.filename.endswith((".mp3", ".wav", ".m4a", ".ogg")):
        return jsonify({"erro": "Formato inválido. Use MP3, WAV, M4A ou OGG"}), 400
    musica_id = f"mus_{current_user.id}_{uuid.uuid4().hex[:8]}"
    filename = f"{musica_id}.mp3"
    filepath = os.path.join(MUSICAS_FOLDER, filename)
    musica.save(filepath)
    # Salvar na lista do usuário (usando sqlite direto)
    try:
        conn = sqlite3.connect('instance/veo3.db')
        try: conn.execute("CREATE TABLE IF NOT EXISTS musicas (id INTEGER PRIMARY KEY, user_id INTEGER, nome TEXT, path TEXT, criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        except: pass
        conn.execute("INSERT INTO musicas (user_id, nome, path) VALUES (?, ?, ?)", (current_user.id, nome, filepath))
        conn.commit()
        conn.close()
    except: pass
    return jsonify({"ok": True, "nome": nome})

@app.route("/minhas_musicas")
@login_required
def minhas_musicas():
    try:
        conn = sqlite3.connect('instance/veo3.db')
        try: conn.execute("CREATE TABLE IF NOT EXISTS musicas (id INTEGER PRIMARY KEY, user_id INTEGER, nome TEXT, path TEXT, criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        except: pass
        rows = conn.execute("SELECT id, nome, path FROM musicas WHERE user_id=? ORDER BY id DESC", (current_user.id,)).fetchall()
        conn.close()
        musicas = [{"id": r[0], "nome": r[1], "path": os.path.basename(r[2])} for r in rows if os.path.exists(r[2])]
        return jsonify({"musicas": musicas})
    except:
        return jsonify({"musicas": []})

@app.route("/deletar_musica", methods=["POST"])
@login_required
def deletar_musica():
    musica_id = request.json.get("id")
    try:
        conn = sqlite3.connect('instance/veo3.db')
        row = conn.execute("SELECT path FROM musicas WHERE id=? AND user_id=?", (musica_id, current_user.id)).fetchone()
        if row and os.path.exists(row[0]):
            os.remove(row[0])
        conn.execute("DELETE FROM musicas WHERE id=? AND user_id=?", (musica_id, current_user.id))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except:
        return jsonify({"erro": "Erro ao deletar"}), 500

@app.route("/musica_file/<path:filename>")
@login_required
def musica_file(filename):
    return send_file(os.path.join(MUSICAS_FOLDER, filename))

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

    if melhorar and current_user.get_provider() == "openai" and current_user.get_api_key():
        linhas = dividir_roteiro(texto, current_user.get_api_key())
    else:
        linhas = [l.strip() for l in texto.replace(",", "\n").replace(".", "\n").split("\n") if l.strip()] or [texto.strip()]

    cenas = [{"index": i+1, "texto": l, "preenchida": False} for i, l in enumerate(linhas)]
    return jsonify({"cenas": cenas, "total": len(cenas)})

@app.route("/gerar_storyboard", methods=["POST"])
@login_required
def gerar_storyboard_route():
    # Plano api_propria exige chave do usuário
    if current_user.plano == "api_propria" and not current_user.api_key:
        return jsonify({"erro": "No plano API Própria, configure sua chave de API no perfil"}), 400
    # Verificar se tem alguma chave disponível (própria ou do sistema)
    if not current_user.get_api_key():
        return jsonify({"erro": "Sistema temporariamente indisponível. Tente novamente mais tarde."}), 400

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

    direcao_criativa = request.form.get("direcao_criativa", "").strip()
    formato = request.form.get("formato", "vertical")

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "aguardando", "progresso": "Na fila...", "total": 0, "atual": 0}
    thread = threading.Thread(target=gerar_storyboard, args=(job_id, current_user.id, texto, estilo, melhorar, False, cenas_preenchidas, direcao_criativa, formato))
    thread.daemon = True
    thread.start()
    return jsonify({"job_id": job_id})

@app.route("/storyboard_img/<sb_id>/<filename>")
@login_required
def storyboard_img(sb_id, filename):
    return send_file(os.path.join(STORYBOARD_FOLDER, sb_id, filename))

@app.route("/rascunhos")
@login_required
def listar_rascunhos():
    """Lista storyboards salvos do usuário"""
    rascunhos = []
    if os.path.exists(STORYBOARD_FOLDER):
        for sb_id in os.listdir(STORYBOARD_FOLDER):
            sb_path = os.path.join(STORYBOARD_FOLDER, sb_id, "storyboard.json")
            if os.path.exists(sb_path):
                try:
                    with open(sb_path) as f:
                        sb_data = json.load(f)
                    blocos = sb_data.get("blocos", [])
                    if not blocos:
                        continue
                    # Verificar se tem pelo menos 1 imagem
                    primeira_img = os.path.join(STORYBOARD_FOLDER, sb_id, blocos[0].get("img", ""))
                    if not os.path.exists(primeira_img):
                        continue
                    # Pegar data de modificação
                    mtime = os.path.getmtime(sb_path)
                    data = datetime.fromtimestamp(mtime).strftime('%d/%m/%Y %H:%M')
                    texto_preview = blocos[0].get("texto", "")[:60]
                    rascunhos.append({
                        "sb_id": sb_id,
                        "total_cenas": len(blocos),
                        "estilo": sb_data.get("estilo", ""),
                        "data": data,
                        "preview": texto_preview,
                        "thumb": blocos[0].get("img", "")
                    })
                except: continue
    rascunhos.sort(key=lambda x: x["data"], reverse=True)
    return jsonify({"rascunhos": rascunhos[:20]})

@app.route("/carregar_rascunho/<sb_id>")
@login_required
def carregar_rascunho(sb_id):
    """Carrega um storyboard salvo"""
    sb_path = os.path.join(STORYBOARD_FOLDER, sb_id, "storyboard.json")
    if not os.path.exists(sb_path):
        return jsonify({"erro": "Rascunho não encontrado"}), 404
    with open(sb_path) as f:
        sb_data = json.load(f)
    return jsonify({"sb_id": sb_id, "blocos": sb_data.get("blocos", []), "estilo": sb_data.get("estilo", "")})

@app.route("/regerar_cena", methods=["POST"])
@login_required
def regerar_cena():
    if not current_user.get_api_key():
        return jsonify({"erro": "Nenhuma chave de API disponível"}), 400
    # Cobrar crédito por regeneração (só imagem base)
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
        if melhorar and current_user.get_provider() == "openai":
            prompt = melhorar_prompt(texto, estilo, current_user.get_api_key())
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
    musica_id = request.form.get("musica_id", "").strip()
    musica_path = ""
    if musica_id:
        try:
            conn = sqlite3.connect('instance/veo3.db')
            row = conn.execute("SELECT path FROM musicas WHERE id=? AND user_id=?", (int(musica_id), current_user.id)).fetchone()
            conn.close()
            if row and os.path.exists(row[0]):
                musica_path = row[0]
        except: pass
    import sys
    sys.stderr.write(f"[ROTA] animar_ia={animar_ia}, musica={musica_path or 'nenhuma'}\n")
    sys.stderr.flush()
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "aguardando", "progresso": "Na fila...", "total": 0, "atual": 0}
    thread = threading.Thread(target=finalizar_video, args=(job_id, current_user.id, sb_id, voice_id, modo_video, legenda_cfg, intervalo, animar_ia, musica_path))
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
        # Migrate: adicionar colunas novas se não existirem
        try:
            conn = sqlite3.connect('instance/veo3.db')
            try: conn.execute("ALTER TABLE user ADD COLUMN banco_ativo BOOLEAN DEFAULT 0")
            except: pass
            conn.commit()
            conn.close()
        except: pass
    app.run(host="0.0.0.0", port=5000)
else:
    # Gunicorn: migrate automático
    with app.app_context():
        try:
            conn = sqlite3.connect('instance/veo3.db')
            try: conn.execute("ALTER TABLE user ADD COLUMN banco_ativo BOOLEAN DEFAULT 0")
            except: pass
            conn.commit()
            conn.close()
        except: pass
