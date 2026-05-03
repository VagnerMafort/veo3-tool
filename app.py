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
import time as _time_module

# Carregar .env antes de tudo
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "veo3-secret-key-mude-isso")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///veo3.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024

# Cloudflare/proxy: confiar nos headers de IP real
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

def get_real_ip():
    """Pega IP real do usuário (Cloudflare → X-Forwarded-For → remote_addr)"""
    return request.headers.get('CF-Connecting-IP') or request.headers.get('X-Real-IP') or request.remote_addr

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"

@login_manager.unauthorized_handler
def unauthorized():
    if request.is_json or request.headers.get('Content-Type', '').startswith('application/json') or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({"erro": "Sessão expirada. Faça login novamente."}), 401
    return redirect(url_for("login"))

# ── Rate Limiting simples ────────────────────────────────
_rate_limits = {}

def rate_limit_check(key, max_requests=5, window=60):
    """Retorna True se passou do limite. key = IP ou user_id"""
    now = _time_module.time()
    if key not in _rate_limits:
        _rate_limits[key] = []
    # Limpar requests antigos
    _rate_limits[key] = [t for t in _rate_limits[key] if now - t < window]
    if len(_rate_limits[key]) >= max_requests:
        return True
    _rate_limits[key].append(now)
    return False

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

# Admin Master — único que pode conceder/remover admin de outros
ADMIN_MASTER_EMAIL = os.environ.get("ADMIN_MASTER_EMAIL", "ministerioprvagner@gmail.com")

def enviar_email(destinatario, assunto, corpo_html):
    """Envia email em background"""
    if not SMTP_USER or not SMTP_PASS:
        return
    def _enviar():
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = assunto
            msg["From"] = f"Klyonclaw Studio <{EMAIL_FROM}>"
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

# Add-on Banco de Áudio
AUDIO_ADDON_PRICE_ID = "price_1TRh4CLW3ZSF3MIlUViqvFjE"
AUDIO_ADDON_VALOR = "R$4,99/mês"

PLANOS_STRIPE = {
    "api_propria": {
        "nome": "API Própria",
        "price_id": "price_1TQXMiLW3ZSF3MIlNx3gwlRh",
        "creditos": 0,
        "valor": "R$87,90/mês",
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
    "tester": {
        "nome": "Tester",
        "price_id": "",
        "creditos": 200,
        "valor": "Grátis",
        "tipo": "interno",
        "descricao": "Plano de teste — concedido pelo admin"
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
PRICE_MAP[AUDIO_ADDON_PRICE_ID] = {"nome": "Banco de Áudio", "key": "audio", "tipo": "addon", "creditos": 0}

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
6. Do NOT add descriptions, visual details, or embellishments.
7. CRITICAL: Maintain the EXACT chronological order of the narrative. NEVER reorder scenes. The first scene in the output must correspond to the first event in the text, and so on.
8. Even if a scene seems out of context, keep it in its original position in the narrative sequence."""
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
    audio_ativo = db.Column(db.Boolean, default=False)
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
        """Retorna chave OpenAI — só se tiver plano ativo, for admin, ou trial"""
        if self.is_admin:
            return self.api_key if self.api_key else SYSTEM_OPENAI_KEY
        if not self.plano:
            # Trial: permitir até 2 imagens grátis
            return SYSTEM_OPENAI_KEY if SYSTEM_OPENAI_KEY else ""
        if self.plano == "api_propria" and self.api_key:
            return self.api_key
        # Planos com créditos usam chave do sistema
        return SYSTEM_OPENAI_KEY

    def get_provider(self):
        """Retorna provider — só se tiver plano ativo, for admin, ou trial"""
        if self.is_admin:
            return self.provider if self.provider else ("openai" if SYSTEM_OPENAI_KEY else "")
        if not self.plano:
            # Trial: usar openai do sistema
            return "openai" if SYSTEM_OPENAI_KEY else ""
        if self.provider:
            return self.provider
        return "openai" if SYSTEM_OPENAI_KEY else ""

    def get_minimax_key(self):
        """Retorna chave MiniMax — só se tiver plano ativo ou for admin"""
        if self.is_admin:
            return self.minimax_key if self.minimax_key else SYSTEM_MINIMAX_KEY
        if not self.plano:
            return ""
        return self.minimax_key if self.minimax_key else SYSTEM_MINIMAX_KEY

    def get_minimax_group_id(self):
        """Retorna Group ID — só se tiver plano ativo ou for admin"""
        if self.is_admin:
            return self.minimax_group_id if self.minimax_group_id else SYSTEM_MINIMAX_GROUP_ID
        if not self.plano:
            return ""
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
MUSICAS_SISTEMA_FOLDER = "musicas_sistema"
os.makedirs(MUSICAS_FOLDER, exist_ok=True)
os.makedirs(MUSICAS_SISTEMA_FOLDER, exist_ok=True)

# Jamendo API (músicas royalty-free)
JAMENDO_CLIENT_ID = os.environ.get("JAMENDO_CLIENT_ID", "709fa152")


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

def melhorar_prompt(texto, estilo, api_key, contexto_roteiro="", ficha_personagens="", direcao_criativa="", tipo_plano=""):
    prompts = load_prompts()
    estilo_det = ESTILOS_DETALHADOS.get(estilo, estilo) if estilo else "photorealistic, natural lighting, high quality, vertical composition"
    try:
        import sys
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

        if ficha_personagens:
            plano_instrucao = f"CAMERA: {tipo_plano} shot." if tipo_plano else ""

            system = f"""Convert this scene into an image prompt in ENGLISH. The scene text is the ONLY thing that matters.

MANDATORY STYLE (you MUST use this exact style, do NOT change it): {estilo_det}
{plano_instrucao}

Character reference (use ONLY when that specific character is mentioned in the scene):
{ficha_personagens}

{f'MANDATORY VISUAL DIRECTION (you MUST set the scene in this environment): {direcao_criativa}. ALL scenes MUST show this visual direction as the setting/background. Do NOT use generic landscapes, mountains or deserts unless the creative direction specifically asks for them.' if direcao_criativa else ''}

STRICT RULES:
- START the prompt with the style description: "{estilo_det}"
- ONLY describe what the scene text says. Nothing else.
- {'The visual environment MUST match the creative direction above. Use luxury, urban, modern settings as described.' if direcao_criativa else ''}
- If the scene is a call to action (comment, share) → show an inspirational scene matching the creative direction, NO people.
- Include a character's physical description ONLY if that character is explicitly mentioned or implied in THIS scene.
- Do NOT add characters or elements from OTHER scenes.
- NEVER use "oil painting", "watercolor", "illustration" or any style other than the MANDATORY STYLE above.
- End with: no text, no letters, no words, no writing, no watermarks
- Output ONLY the prompt."""

        else:
            system = prompts.get("melhorar", DEFAULT_PROMPTS["melhorar"]).replace("{estilo}", estilo_det)
            if direcao_criativa:
                system += f"\n\nMANDATORY VISUAL DIRECTION: \"{direcao_criativa}\". ALL scenes MUST use this as the visual environment/setting. Do NOT use generic landscapes, mountains or deserts unless specifically asked."
            if contexto_roteiro:
                system += f"\n\nFull story context: \"{contexto_roteiro[:300]}\"\nKeep ALL characters visually identical across scenes."

        body = {"model": "gpt-4o-mini", "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Scene: {texto}"}
        ], "max_tokens": 600}
        r = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=body, timeout=30)
        if r.ok:
            resultado = r.json()["choices"][0]["message"]["content"].strip()
            sys.stderr.write(f"[PROMPT] Cena: {texto[:50]}... → {resultado[:100]}...\n"); sys.stderr.flush()
            return resultado
    except: pass
    return f"{estilo_det} of {texto}, no text no words"


def planejar_planos_camera(linhas, api_key):
    """GPT define o tipo de plano (wide/medium/close) pra cada cena de uma vez, garantindo variedade"""
    try:
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        cenas_texto = "\n".join([f"{i+1}. {l}" for i, l in enumerate(linhas)])
        body = {"model": "gpt-4o-mini", "messages": [
            {"role": "system", "content": """You are a film director planning camera shots for a storyboard.
For each scene, assign ONE camera shot type. You MUST vary the shots — never use the same type more than 2 times in a row.

Shot types:
- WIDE PANORAMIC: armies, landscapes, epic supernatural events, establishing shots, large-scale action
- MEDIUM: dialogue between characters, interactions, walking, normal actions
- CLOSE-UP: intense emotions, prayers, whispers (use MAX 2 in the entire video)
- LOW ANGLE: powerful/intimidating subjects, looking up at something massive
- BIRD'S EYE: showing scale, a city surrounded, overview

Rules:
- First scene should be WIDE (establishing)
- Scenes with armies/supernatural = WIDE PANORAMIC
- Scenes with dialogue = MEDIUM
- Scenes that are calls to action (comment, share, subscribe) = WIDE PANORAMIC (inspirational landscape)
- Output ONLY the shot type for each scene, one per line, in order. Example:
WIDE PANORAMIC
MEDIUM
CLOSE-UP
MEDIUM
WIDE PANORAMIC"""},
            {"role": "user", "content": cenas_texto}
        ], "max_tokens": 200}
        r = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=body, timeout=15)
        if r.ok:
            resultado = r.json()["choices"][0]["message"]["content"].strip()
            planos = [l.strip() for l in resultado.split("\n") if l.strip()]
            # Garantir que tem o mesmo número de planos que cenas
            while len(planos) < len(linhas):
                planos.append("MEDIUM")
            return planos[:len(linhas)]
    except: pass
    # Fallback: alternar wide/medium
    return [("WIDE PANORAMIC" if i % 3 == 0 else "MEDIUM") for i in range(len(linhas))]


def gerar_imagem_referencia(ficha, estilo, api_key, output_path, formato="vertical"):
    """Gera uma imagem de referência com todos os personagens juntos — não entra no vídeo"""
    import sys
    estilo_det = ESTILOS_DETALHADOS.get(estilo, estilo) if estilo else "photorealistic, natural lighting, high quality"
    try:
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        body = {"model": "gpt-4o-mini", "messages": [
            {"role": "system", "content": f"""Create an image prompt for a CHARACTER REFERENCE SHEET showing ALL characters from this story standing side by side.

Art style: {estilo_det}

Character descriptions:
{ficha}

Rules:
1. Show ALL characters standing next to each other, facing the camera, full body visible
2. Well-lit scene with bright warm lighting — NOT dark
3. Each character in their exact described clothing and appearance
4. Simple neutral background (stone wall, desert, sky)
5. This is a reference sheet — characters should be clearly visible and distinguishable
6. End with: no text, no letters, no words, no writing, no watermarks
7. Output ONLY the prompt."""},
            {"role": "user", "content": "Generate the character reference sheet prompt."}
        ], "max_tokens": 400}
        r = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=body, timeout=15)
        if r.ok:
            ref_prompt = r.json()["choices"][0]["message"]["content"].strip()
            sys.stderr.write(f"[REF] Gerando imagem de referência...\n"); sys.stderr.flush()
            size = "1024x1792" if formato == "vertical" else ("1792x1024" if formato == "horizontal" else "1024x1024")
            gerar_imagem_openai(ref_prompt, api_key, size, "standard", output_path, modelo="gpt-image-1")
            sys.stderr.write(f"[REF] Imagem de referência gerada\n"); sys.stderr.flush()
            return True
    except Exception as e:
        sys.stderr.write(f"[REF] Erro ao gerar referência: {e}\n"); sys.stderr.flush()
    return False


def eh_cena_nao_visual(texto):
    """Detecta se a cena é uma call-to-action (não visual) — comenta, compartilha, inscreva-se"""
    texto_lower = texto.lower()
    palavras_cta = ["comenta", "compartilha", "inscreva", "subscribe", "comment", "share", "like",
                    "curta", "siga", "segue", "deixa o like", "ativa o sininho", "se inscreva"]
    return any(p in texto_lower for p in palavras_cta)

def extrair_personagens(roteiro, api_key, estilo=""):
    """Extrai ficha de personagens organizada por hierarquia"""
    estilo_det = ESTILOS_DETALHADOS.get(estilo, estilo) if estilo else "default style"
    try:
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        system = f"""You are a character designer creating a STRICT visual reference sheet for an AI image generator.
The art style is: {estilo_det}

Analyze the story and create a hierarchical reference sheet. ONLY include elements that actually exist in the story.

OUTPUT FORMAT (use ONLY the sections that apply):

1. MAIN CHARACTER: [name] — [ultra-detailed visual description: face, eyes, hair, skin, clothing with hex colors, body type, age, distinguishing features]

2. MAIN SETTING: [description of the primary location/background that appears most often, with colors and atmosphere]

3. SECONDARY CHARACTER 1: [name] — [detailed visual description] (ONLY if the story has a second character)

4. SECONDARY CHARACTER 2: [name] — [detailed visual description] (ONLY if the story has a third character)

5. SECONDARY SETTING: [description of secondary location] (ONLY if the story changes location)

ART_STYLE: [exact art style rules for ALL images — line thickness, color palette, shading, lighting]

RULES:
- If the story has ONLY ONE character, do NOT invent secondary characters. Skip sections 3 and 4.
- If the story happens in ONE location, do NOT invent secondary settings. Skip section 5.
- FACE: exact eye shape, size, color, eyebrow shape, nose, mouth. For cartoon: specify exact eye style.
- HAIR: exact color (hex), length, style
- CLOTHING: exact colors with hex codes, exact garment types. MUST NOT change between scenes.
- SKIN: exact skin tone that stays consistent
- Write in ENGLISH
- Be so specific that the same character looks IDENTICAL in every single frame
- Max 800 tokens total"""
        body = {"model": "gpt-4o-mini", "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": roteiro}
        ], "max_tokens": 800}
        r = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=body, timeout=30)
        if r.ok:
            return r.json()["choices"][0]["message"]["content"].strip()
    except: pass
    return ""

def gerar_audio_minimax(texto, api_key, group_id, voice_id, output_path, speed=1.0):
    url = f"https://api.minimaxi.chat/v1/t2a_v2?GroupId={group_id}"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {"model": "speech-02-hd", "text": texto, "stream": False,
            "voice_setting": {"voice_id": voice_id, "speed": speed, "vol": 1.0, "pitch": 0},
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
    try:
        data = json.loads(r.text.split('\n')[0]) if '\n' in r.text else r.json()
    except:
        # Tentar extrair JSON válido do início da resposta
        try:
            import re as _re
            match = _re.search(r'\{.*?\}', r.text, _re.DOTALL)
            data = json.loads(match.group()) if match else r.json()
        except:
            data = r.json()
    task_id = data.get("task_id")
    if not task_id:
        resp_str = str(data)
        sys.stderr.write(f"[VIDEO] Sem task_id. Resposta: {resp_str[:300]}\n"); sys.stderr.flush()
        if "1008" in resp_str or "insufficient" in resp_str.lower():
            raise Exception("SALDO_MINIMAX:Saldo insuficiente na conta MiniMax. Recarregue em platform.minimaxi.com")
        if "1002" in resp_str or "rate" in resp_str.lower():
            raise Exception("RATE_LIMIT:Estamos com uma alta demanda no momento. Por favor, tente novamente mais tarde.")
        raise Exception("Estamos com problemas técnicos na animação. Por favor, tente novamente mais tarde.")
    sys.stderr.write(f"[VIDEO] Task criada: {task_id}\n"); sys.stderr.flush()

    # Poll status (a cada 5s, max 10 min)
    for _ in range(120):  # max 10 min
        time.sleep(5)
        try:
            r2 = requests.get("https://api.minimax.io/v1/query/video_generation",
                              headers=headers, params={"task_id": task_id}, timeout=15)
            if not r2.ok:
                continue
            try:
                status_data = json.loads(r2.text.split('\n')[0]) if '\n' in r2.text else r2.json()
            except:
                try:
                    import re as _re
                    match = _re.search(r'\{.*\}', r2.text, _re.DOTALL)
                    status_data = json.loads(match.group()) if match else r2.json()
                except:
                    continue
            status = status_data.get("status", "")
            if status == "Success":
                file_id = status_data.get("file_id")
                if not file_id:
                    raise Exception("Estamos com problemas técnicos na animação. Por favor, tente novamente mais tarde.")
                r3 = requests.get("https://api.minimax.io/v1/files/retrieve",
                                  headers=headers, params={"file_id": file_id}, timeout=15)
                if not r3.ok:
                    raise Exception("Estamos com problemas técnicos na animação. Por favor, tente novamente mais tarde.")
                try:
                    r3_data = json.loads(r3.text.split('\n')[0]) if '\n' in r3.text else r3.json()
                except:
                    r3_data = r3.json()
                download_url = r3_data.get("file", {}).get("download_url")
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
    # Marca d'água em imagens (exceto api_propria, business e admin)
    if not usuario_sem_marca(user):
        aplicar_marca_dagua_imagem(output_path)
    salvar_no_banco(prompt, estilo, output_path, tipo="imagem")


def usuario_sem_marca(user):
    """Retorna True se o usuário NÃO deve ter marca d'água"""
    if user.is_admin:
        return True
    if user.plano in ("api_propria", "business"):
        return True
    return False


def aplicar_marca_dagua_imagem(img_path):
    """Adiciona marca d'água semi-transparente na imagem"""
    try:
        from PIL import ImageDraw, ImageFont
        img = Image.open(img_path).convert("RGBA")
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        texto = "Klyonclaw Studio"
        # Tamanho da fonte proporcional à imagem
        font_size = max(20, img.width // 18)
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
        except:
            try:
                font = ImageFont.truetype("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf", font_size)
            except:
                font = ImageFont.load_default()
        bbox = draw.textbbox((0, 0), texto, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        # Posição: centro inferior
        x = (img.width - tw) // 2
        y = img.height - th - max(30, img.height // 20)
        # Sombra
        draw.text((x + 2, y + 2), texto, fill=(0, 0, 0, 80), font=font)
        # Texto semi-transparente
        draw.text((x, y), texto, fill=(255, 255, 255, 90), font=font)
        result = Image.alpha_composite(img, overlay).convert("RGB")
        result.save(img_path)
    except Exception as e:
        import sys
        sys.stderr.write(f"[WATERMARK] Erro imagem: {e}\n"); sys.stderr.flush()


def get_marca_dagua_ffmpeg():
    """Retorna o filtro FFmpeg para marca d'água em vídeo"""
    return "drawtext=text='Klyonclaw Studio':fontsize=28:fontcolor=white@0.3:x=(w-text_w)/2:y=h-th-30:shadowcolor=black@0.3:shadowx=2:shadowy=2"

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
        # Medir duração real do áudio pra usar como referência (evita -shortest que dessincroniza)
        try:
            p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
                               capture_output=True, text=True)
            dur_audio = float(p.stdout.strip()) if p.returncode == 0 else 0
        except:
            dur_audio = 0
        cmd += ["-map", f"{len(imagens)}:a", "-c:a", "aac", "-b:a", "192k"]
        if dur_audio > 0:
            cmd += ["-t", str(dur_audio)]
        else:
            cmd += ["-shortest"]
    cmd += ["-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p", output_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise Exception(f"FFmpeg erro: {result.stderr[-800:]}")

def limpar_job(job_dir):
    try:
        if os.path.exists(job_dir): shutil.rmtree(job_dir)
    except: pass

def dividir_roteiro(texto, api_key, tipo_video="estatico"):
    # Texto muito curto (1 frase simples) = não divide
    if len(texto) < 30:
        return [texto.strip()]

    # Estimar duração do áudio: ~2.5 palavras/segundo em português
    n_palavras = len(texto.split())
    duracao_estimada = n_palavras / 2.5  # segundos

    prompts = load_prompts()
    try:
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        system = prompts.get("dividir", DEFAULT_PROMPTS["dividir"])

        if tipo_video == "animado":
            # Cada cena deve ter 4-6s de narração (~2.5 palavras/s com speed 1.1 = ~12-16 palavras)
            MAX_PALAVRAS_CENA = 15  # ~6s de narração
            MIN_PALAVRAS_CENA = 10  # ~4s de narração
            cenas_alvo = max(3, round(n_palavras / 13))  # ~13 palavras por cena = ~5s
            # Dividir no código — não confiar no GPT pra manter texto completo
            # Separar por frases (pontuação)
            import re
            frases = re.split(r'(?<=[.!?;])\s+', texto.strip())
            frases = [f.strip() for f in frases if f.strip()]

            if len(frases) <= cenas_alvo:
                linhas_resultado = frases[:]
            else:
                # Agrupar frases respeitando o limite de palavras por cena
                palavras_por_cena = min(MAX_PALAVRAS_CENA, max(MIN_PALAVRAS_CENA, n_palavras // cenas_alvo))
                linhas_resultado = []
                cena_atual = ""
                for frase in frases:
                    if cena_atual and len((cena_atual + " " + frase).split()) > palavras_por_cena:
                        linhas_resultado.append(cena_atual.strip())
                        cena_atual = frase
                    else:
                        cena_atual = (cena_atual + " " + frase).strip() if cena_atual else frase
                if cena_atual:
                    linhas_resultado.append(cena_atual.strip())

            # Quebrar cenas que ainda estão muito longas (>MAX_PALAVRAS_CENA)
            resultado_final = []
            for cena in linhas_resultado:
                palavras = cena.split()
                if len(palavras) > MAX_PALAVRAS_CENA:
                    # Dividir num ponto natural
                    meio = len(palavras) // 2
                    melhor_corte = meio
                    for j in range(max(3, meio - 5), min(len(palavras) - 2, meio + 6)):
                        if palavras[j].endswith(('.', ',', ';', '!', '?')):
                            melhor_corte = j + 1
                            break
                    resultado_final.append(" ".join(palavras[:melhor_corte]))
                    resultado_final.append(" ".join(palavras[melhor_corte:]))
                else:
                    resultado_final.append(cena)
            linhas_resultado = resultado_final

            # Juntar cenas muito curtas (<MIN_PALAVRAS_CENA) com a próxima
            resultado_final = []
            i = 0
            while i < len(linhas_resultado):
                cena = linhas_resultado[i]
                if len(cena.split()) < MIN_PALAVRAS_CENA and i + 1 < len(linhas_resultado):
                    # Juntar com a próxima se o total não passar do máximo
                    proxima = linhas_resultado[i + 1]
                    if len(cena.split()) + len(proxima.split()) <= MAX_PALAVRAS_CENA:
                        resultado_final.append((cena + " " + proxima).strip())
                        i += 2
                        continue
                resultado_final.append(cena)
                i += 1
            linhas_resultado = resultado_final

            import sys
            sys.stderr.write(f"[DIVIDIR] Animado: {n_palavras} palavras, {int(duracao_estimada)}s, alvo {cenas_alvo} cenas, resultado {len(linhas_resultado)} cenas\n"); sys.stderr.flush()
            return linhas_resultado
        else:
            max_cenas = max(2, min(60, len(texto) // 20))
            system += f"""

IMPORTANT: Create a MAXIMUM of {max_cenas} scenes. Each scene must be a meaningful story beat. You MUST include ALL parts of the text from beginning to end - do NOT skip, cut, or omit ANY part. The ENTIRE text must be covered. Every sentence of the original text MUST appear in at least one scene."""

        body = {"model": "gpt-4o-mini", "messages": [
            {"role": "system", "content": system}, {"role": "user", "content": texto}
        ], "max_tokens": 8000}
        r = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=body, timeout=60)
        if r.ok:
            resultado = r.json()["choices"][0]["message"]["content"].strip()
            linhas = [l.strip() for l in resultado.split("\n") if l.strip()]
            import sys
            sys.stderr.write(f"[DIVIDIR] tipo={tipo_video}, palavras={n_palavras}, GPT retornou {len(linhas)} cenas\n"); sys.stderr.flush()
            if len(linhas) > max_cenas:
                linhas = linhas[:max_cenas]
            if len(linhas) >= 1:
                return linhas
    except: pass
    return [l.strip() for l in texto.replace(",", "\n").replace(".", "\n").split("\n") if l.strip()] or [texto.strip()]

def gerar_storyboard(job_id, user_id, texto_manual, estilo, melhorar_prompts, usar_banco=False, cenas_preenchidas=None, direcao_criativa="", formato="vertical", personagem_path="", tipo_video="estatico"):
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
                linhas = dividir_roteiro(texto_manual, user.get_api_key(), tipo_video)
            else:
                linhas = [l.strip() for l in texto_manual.replace(",", "\n").replace(".", "\n").split("\n") if l.strip()]

            total = len(linhas)
            # Contar quantas cenas precisam ser geradas (excluir preenchidas)
            cenas_a_gerar = [i for i in range(total) if str(i+1) not in cenas_preenchidas]

            # Trial: usuário sem plano pode gerar até 2 imagens grátis
            is_trial = not user.plano and not user.is_admin
            TRIAL_MAX_IMAGENS = 2
            # Registrar ação
            try:
                conn_a = sqlite3.connect('instance/veo3.db')
                conn_a.execute("CREATE TABLE IF NOT EXISTS user_acoes (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, acao TEXT, detalhe TEXT, criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
                conn_a.execute("INSERT INTO user_acoes (user_id, acao, detalhe) VALUES (?, ?, ?)", (user.id, "gerar_imagens", f"{len(cenas_a_gerar)} cenas"))
                conn_a.commit(); conn_a.close()
            except: pass
            if is_trial:
                # Contar imagens já geradas pelo trial (criações anteriores)
                trial_geradas = Criacao.query.filter_by(user_id=user.id).count()
                imagens_restantes = max(0, TRIAL_MAX_IMAGENS - trial_geradas)
                if imagens_restantes <= 0:
                    jobs[job_id] = {"status": "erro", "progresso": "TRIAL_LIMITE:Você já usou suas 2 imagens grátis. Assine um plano para continuar criando.", "total": 0, "atual": 0}
                    return
                # Limitar cenas a gerar ao restante do trial
                cenas_a_gerar = cenas_a_gerar[:imagens_restantes]
                creditos_necessarios = 0  # Trial não cobra créditos
            else:
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

            # ── ETAPA 0: Preparação (sequencial pra evitar deadlock) ──
            ficha = ""
            ref_img_path = ""
            planos_camera = []
            if melhorar_prompts and user.get_provider() == "openai" and cenas_a_gerar:
                import sys
                sys.stderr.write(f"[PREP] Iniciando preparação...\n"); sys.stderr.flush()
                jobs[job_id]["progresso"] = "Analisando personagens..."

                # Se tem foto de referência do usuário
                if personagem_path and os.path.exists(personagem_path):
                    try:
                        import base64 as b64mod
                        with open(personagem_path, "rb") as fref:
                            ref_b64 = b64mod.b64encode(fref.read()).decode()
                        headers_vis = {"Authorization": f"Bearer {user.get_api_key()}", "Content-Type": "application/json"}
                        body_vis = {"model": "gpt-4o-mini", "messages": [
                            {"role": "system", "content": """Analyze this reference image. Describe EACH character with extreme detail.
Include: skin tone, hair (color hex, style, length), eyes (color, shape), clothing (colors with hex, garment types), body type, age, features.
Write in ENGLISH. Output ONLY the description."""},
                            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{ref_b64}"}}]}
                        ], "max_tokens": 500}
                        r_vis = requests.post("https://api.openai.com/v1/chat/completions", headers=headers_vis, json=body_vis, timeout=30)
                        if r_vis.ok:
                            ficha_visual = r_vis.json()["choices"][0]["message"]["content"].strip()
                            ficha = f"MAIN CHARACTER (from reference photo):\n{ficha_visual}\n\n"
                            ficha += extrair_personagens(texto_manual, user.get_api_key(), estilo)
                    except Exception as e:
                        sys.stderr.write(f"[PREP] Erro foto ref: {e}\n"); sys.stderr.flush()
                        ficha = extrair_personagens(texto_manual, user.get_api_key(), estilo)
                else:
                    # Extrair ficha textual
                    sys.stderr.write(f"[PREP] Extraindo ficha...\n"); sys.stderr.flush()
                    ficha = extrair_personagens(texto_manual, user.get_api_key(), estilo)
                    sys.stderr.write(f"[PREP] Ficha OK: {len(ficha)} chars\n"); sys.stderr.flush()

                    # Gerar imagem de referência
                    if ficha:
                        jobs[job_id]["progresso"] = "Gerando referência visual..."
                        ref_img_path = os.path.join(sb_dir, "_reference.png")
                        sys.stderr.write(f"[PREP] Gerando imagem referência...\n"); sys.stderr.flush()
                        if not gerar_imagem_referencia(ficha, estilo, user.get_api_key(), ref_img_path, formato):
                            ref_img_path = ""
                            sys.stderr.write(f"[PREP] Referência falhou\n"); sys.stderr.flush()

                    # Analisar referência com visão
                    if ref_img_path and os.path.exists(ref_img_path):
                        try:
                            import base64 as b64mod
                            with open(ref_img_path, "rb") as fref:
                                ref_b64 = b64mod.b64encode(fref.read()).decode()
                            headers_vis = {"Authorization": f"Bearer {user.get_api_key()}", "Content-Type": "application/json"}
                            body_vis = {"model": "gpt-4o-mini", "messages": [
                                {"role": "system", "content": """This is a character reference sheet. Describe EACH character with extreme detail.
For each: skin tone, hair (color hex, style), eyes (color, shape), clothing (colors hex, garments), body type, age, features.
Format: CHARACTER 1: [description] | CHARACTER 2: [description]
Write in ENGLISH."""},
                                {"role": "user", "content": [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{ref_b64}"}}]}
                            ], "max_tokens": 600}
                            r_vis = requests.post("https://api.openai.com/v1/chat/completions", headers=headers_vis, json=body_vis, timeout=30)
                            if r_vis.ok:
                                ficha_visual = r_vis.json()["choices"][0]["message"]["content"].strip()
                                ficha += f"\n\nVISUAL REFERENCE (use these EXACT details):\n{ficha_visual}"
                                sys.stderr.write(f"[PREP] Ficha complementada\n"); sys.stderr.flush()
                        except Exception as e:
                            sys.stderr.write(f"[PREP] Erro análise ref: {e}\n"); sys.stderr.flush()

                # Planos de câmera
                sys.stderr.write(f"[PREP] Planejando planos de câmera...\n"); sys.stderr.flush()
                planos_camera = planejar_planos_camera(linhas, user.get_api_key())
                sys.stderr.write(f"[PREP] Planos OK: {planos_camera}\n"); sys.stderr.flush()

            # ── ETAPA 2: Gerar imagens em paralelo (geração NORMAL, não edits) ──
            def gerar_bloco(i_linha):
                import sys
                linha = linhas[i_linha]
                img_path = os.path.join(sb_dir, f"{i_linha+1:03d}.png")

                # Detectar cena não-visual (CTA: comenta, compartilha, etc.)
                if eh_cena_nao_visual(linha):
                    estilo_det = ESTILOS_DETALHADOS.get(estilo, estilo) if estilo else "cinematic"
                    fallback_prompt = f"{estilo_det}, inspirational landscape, golden sunrise over mountains, warm light rays through clouds, hope and faith, epic wide panoramic shot, bright colors, no text, no letters, no words, no watermarks"
                    try:
                        gerar_imagem_openai(fallback_prompt, user.get_api_key(),
                                            "1024x1792" if formato == "vertical" else ("1792x1024" if formato == "horizontal" else "1024x1024"),
                                            "standard", img_path, modelo="gpt-image-1")
                    except:
                        try:
                            gerar_imagem_openai(fallback_prompt, user.get_api_key(),
                                                "1024x1792" if formato == "vertical" else "1024x1024",
                                                "standard", img_path, modelo="dall-e-3")
                        except: pass
                    blocos.append({"index": i_linha+1, "texto": linha, "img": f"{i_linha+1:03d}.png"})
                    jobs[job_id]["atual"] = len(blocos)
                    jobs[job_id]["progresso"] = f"Gerando imagem {len(blocos)} de {total}..."
                    return

                # Gerar prompt com plano de câmera específico
                tipo_plano = planos_camera[i_linha] if i_linha < len(planos_camera) else "MEDIUM"
                if melhorar_prompts and user.get_provider() == "openai":
                    prompt_final = melhorar_prompt(linha, estilo, user.get_api_key(), roteiro_completo, ficha, direcao_criativa, tipo_plano)
                else:
                    prompt_final = f"{linha}, {estilo}" if estilo else linha

                # Geração normal (rápida, respeita planos de câmera)
                gerado = False
                try:
                    gerar_imagem(prompt_final, user, img_path, estilo, formato=formato)
                    gerado = True
                except Exception as e:
                    sys.stderr.write(f"[IMG] Geração falhou cena {i_linha+1}: {e}\n"); sys.stderr.flush()

                # Fallback: suavizar e tentar
                if not gerado:
                    try:
                        prompt_suave = suavizar_prompt(prompt_final, user.get_api_key())
                        gerar_imagem_openai(prompt_suave, user.get_api_key(),
                                            "1024x1792" if formato == "vertical" else ("1792x1024" if formato == "horizontal" else "1024x1024"),
                                            "standard", img_path, modelo="gpt-image-1")
                        gerado = True
                    except: pass

                # Fallback 2: imagem genérica
                if not gerado:
                    try:
                        estilo_det = ESTILOS_DETALHADOS.get(estilo, estilo) if estilo else "cinematic"
                        gerar_imagem_openai(f"{estilo_det}, ancient Middle Eastern city at dawn, warm golden light, dramatic sky, no text, no words, no watermarks",
                                            user.get_api_key(),
                                            "1024x1792" if formato == "vertical" else "1024x1024",
                                            "standard", img_path, modelo="dall-e-3")
                    except:
                        sys.stderr.write(f"[IMG] TODOS os fallbacks falharam cena {i_linha+1}\n"); sys.stderr.flush()

                # Verificar recusa (imagem muito pequena)
                if os.path.exists(img_path):
                    file_size = os.path.getsize(img_path)
                    if file_size < 20000:
                        sys.stderr.write(f"[IMG] Cena {i_linha+1}: imagem suspeita ({file_size}B), regenerando\n"); sys.stderr.flush()
                        try:
                            estilo_det = ESTILOS_DETALHADOS.get(estilo, estilo) if estilo else "cinematic"
                            gerar_imagem_openai(f"{estilo_det}, dramatic ancient scene, warm golden light, epic atmosphere, no text, no words",
                                                user.get_api_key(), "1024x1792" if formato == "vertical" else "1024x1024",
                                                "standard", img_path, modelo="dall-e-3")
                        except: pass

                blocos.append({"index": i_linha+1, "texto": linha, "img": f"{i_linha+1:03d}.png"})
                jobs[job_id]["atual"] = len(blocos)
                jobs[job_id]["progresso"] = f"Gerando imagem {len(blocos)} de {total}..."

            if cenas_a_gerar:
                with ThreadPoolExecutor(max_workers=5) as executor:
                    list(executor.map(gerar_bloco, cenas_a_gerar))

            # Gastar créditos apenas pelas cenas geradas (trial não cobra)
            creditos_gastos_sb = 0
            if cenas_a_gerar and not is_trial:
                creditos_por_cena = calcular_creditos_cena(melhorar_prompt=melhorar_prompts, narracao=False, animar=False)
                creditos_gastos_sb = len(cenas_a_gerar) * creditos_por_cena
                if not user.gastar_creditos(creditos_gastos_sb):
                    jobs[job_id] = {"status": "erro", "progresso": f"Créditos insuficientes. Necessário: {creditos_gastos_sb}", "total": 0, "atual": 0}
                    return
                db.session.commit()

            blocos.sort(key=lambda x: x["index"])
            sb_data = {"blocos": blocos, "estilo": estilo, "dir": sb_dir, "creditos_gastos": creditos_gastos_sb, "tipo_video": tipo_video, "is_trial": is_trial}
            with open(os.path.join(sb_dir, "storyboard.json"), "w") as f:
                json.dump(sb_data, f)
            jobs[job_id] = {"status": "storyboard_pronto", "progresso": "Storyboard pronto", "total": total, "atual": total, "blocos": blocos, "sb_id": job_id}
        except Exception as e:
            jobs[job_id] = {"status": "erro", "progresso": str(e), "total": 0, "atual": 0}

def finalizar_video(job_id, user_id, sb_id, voice_id, modo_video, legenda_cfg, intervalo, animar_ia=False, musica_path="", efeitos_sonoros=False):
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
            # Registrar ação
            try:
                conn_a = sqlite3.connect('instance/veo3.db')
                conn_a.execute("CREATE TABLE IF NOT EXISTS user_acoes (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, acao TEXT, detalhe TEXT, criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
                conn_a.execute("INSERT INTO user_acoes (user_id, acao, detalhe) VALUES (?, ?, ?)", (user_id, "finalizar_video", f"{len(blocos)} cenas, modo={modo_video}, animar={animar_ia}"))
                conn_a.commit(); conn_a.close()
            except: pass
            # Rastrear créditos gastos (inclui geração de imagens/divisão do storyboard)
            creditos_gastos_video = sb_data.get("creditos_gastos", 0)

            if user.get_minimax_key() and voice_id:
                # Cobrar créditos de narração
                creditos_narracao = len(blocos) * CREDITOS_NARRACAO
                if not user.gastar_creditos(creditos_narracao):
                    jobs[job_id] = {"status": "erro", "progresso": f"Créditos insuficientes para narração. Necessário: {creditos_narracao}", "total": 0, "atual": 0}
                    return
                creditos_gastos_video += creditos_narracao
                db.session.commit()
                jobs[job_id]["progresso"] = "Gerando narração..."
                imagens = []
                t = 0
                import time as _time
                import sys

                # ── NARRAR CADA CENA SEPARADAMENTE ──
                # Garante sincronização perfeita: duração da imagem = duração exata do áudio daquela cena
                audios_cena = []
                for idx_cena, bloco in enumerate(blocos):
                    audio_cena_path = os.path.join(job_dir, f"narr_{idx_cena+1:04d}.mp3")
                    jobs[job_id]["progresso"] = f"Narrando cena {idx_cena+1}/{len(blocos)}..."
                    narracao_ok = False
                    for tentativa in range(3):
                        try:
                            # Velocidade: shorts mais rápido, longo um pouco mais rápido que o padrão
                            narr_speed = 1.2 if modo_video == "shorts" else 1.1
                            gerar_audio_minimax(bloco["texto"], user.get_minimax_key(), user.get_minimax_group_id(), voice_id, audio_cena_path, speed=narr_speed)
                            narracao_ok = True
                            break
                        except Exception as e:
                            if "1002" in str(e) or "rate" in str(e).lower():
                                _time.sleep(10 + tentativa * 10)
                                continue
                            raise
                    if not narracao_ok:
                        jobs[job_id] = {"status": "erro", "progresso": "Estamos com uma alta demanda no momento. Por favor, tente novamente mais tarde.", "total": 0, "atual": 0}
                        return
                    audios_cena.append(audio_cena_path)
                    # Pausa entre chamadas pra evitar rate limit
                    if idx_cena < len(blocos) - 1:
                        _time.sleep(1)

                # ── MEDIR DURAÇÃO REAL + CONCATENAR ──
                jobs[job_id]["progresso"] = "Sincronizando narração com cenas..."
                audio_completo_seg = AudioSegment.empty()
                for idx_cena, bloco in enumerate(blocos):
                    seg = AudioSegment.from_file(audios_cena[idx_cena])

                    # Shorts: remover silêncio de cada cena
                    if modo_video == "shorts":
                        audio_limpo = os.path.join(job_dir, f"narr_limpa_{idx_cena+1:04d}.mp3")
                        remover_silencio(audios_cena[idx_cena], audio_limpo)
                        seg = AudioSegment.from_file(audio_limpo)

                    dur_cena = len(seg) / 1000
                    dur_cena = max(2.0, dur_cena)

                    img_src = os.path.join(sb_dir, bloco["img"])
                    img_dst = os.path.join(job_dir, f"{idx_cena+1:04d}.png")
                    shutil.copy(img_src, img_dst)
                    imagens.append({
                        "index": idx_cena + 1,
                        "path": img_dst,
                        "duracao": round(dur_cena, 2),
                        "inicio": round(t, 2),
                        "fim": round(t + dur_cena, 2),
                        "texto": bloco["texto"]
                    })
                    audio_completo_seg += seg
                    sys.stderr.write(f"[SYNC] Cena {idx_cena+1}: {dur_cena:.2f}s | Acumulado: {t + dur_cena:.2f}s\n"); sys.stderr.flush()
                    t += dur_cena

                # Exportar áudio concatenado
                audio_completo_path = os.path.join(job_dir, "narracao.mp3")
                audio_completo_seg.export(audio_completo_path, format="mp3", bitrate="192k")
                duracao_total = len(audio_completo_seg) / 1000
                sys.stderr.write(f"[SYNC] Total: {len(imagens)} cenas | {duracao_total:.2f}s\n"); sys.stderr.flush()

                audio_final_path = audio_completo_path
            else:
                # Sem narração: 1 imagem por cena, duração = intervalo (mínimo 3s)
                imagens = []
                t = 0
                dur_cena = max(3, intervalo)
                for i, bloco in enumerate(blocos):
                    img_src = os.path.join(sb_dir, bloco["img"])
                    img_dst = os.path.join(job_dir, f"{i+1:04d}.png")
                    shutil.copy(img_src, img_dst)
                    imagens.append({"index": i+1, "path": img_dst, "duracao": dur_cena,
                                    "inicio": round(t, 2), "fim": round(t + dur_cena, 2),
                                    "texto": bloco["texto"]})
                    t += dur_cena

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
                            # Traduzir texto da cena em instrução de movimento para o MiniMax
                            cena_texto = img.get("texto", "")[:120]
                            anim_prompt = f"{cena_texto}. Characters perform the described action with full body movement. Dynamic camera tracking shot. Cinematic motion, dramatic lighting. Never move characters backward unless explicitly stated."
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
                            sys.stderr.write(f"[ANIMAR] Cena {i+1} tentativa {tentativa+1}: {erro_str}\n"); sys.stderr.flush()
                            if "RATE_LIMIT" in erro_str or "1002" in erro_str or "rate" in erro_str.lower():
                                if tentativa < 2:
                                    _t.sleep(30 + tentativa * 15)
                                    continue
                            if "SALDO_MINIMAX" in erro_str or "insufficient" in erro_str.lower():
                                # Saldo insuficiente — parar todas as animações
                                sys.stderr.write(f"[ANIMAR] Saldo MiniMax insuficiente, parando animações\n"); sys.stderr.flush()
                                clipes_video[i] = None
                                return
                            clipes_video[i] = None
                            return
                    clipes_video[i] = None

                # Animar em paralelo (3 por vez)
                import time as _time
                lote_size = 3
                for lote_start in range(0, n_cenas, lote_size):
                    lote_end = min(lote_start + lote_size, n_cenas)
                    lote = list(range(lote_start, lote_end))
                    with ThreadPoolExecutor(max_workers=lote_size) as executor:
                        list(executor.map(animar_cena, lote))
                    if lote_end < n_cenas:
                        _time.sleep(3)

                # Cobrar créditos só pelas animações que deram certo
                cenas_animadas = sum(1 for c in clipes_video if c is not None)
                if cenas_animadas > 0:
                    user.gastar_creditos(cenas_animadas * CREDITOS_ANIMACAO)
                    creditos_gastos_video += cenas_animadas * CREDITOS_ANIMACAO
                    db.session.commit()

                # Se pelo menos 1 clipe foi gerado, montar vídeo final
                if any(clipes_video):
                    jobs[job_id]["progresso"] = "Montando vídeo final..."

                    clipes_validos = []
                    for ci, cp in enumerate(clipes_video):
                        if cp and os.path.exists(cp) and ci < len(imagens):
                            dur_cena_audio = imagens[ci]["duracao"]
                            # Medir duração real do clipe animado
                            try:
                                p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                                    "-of", "default=noprint_wrappers=1:nokey=1", cp],
                                                   capture_output=True, text=True)
                                dur_clipe = float(p.stdout.strip()) if p.returncode == 0 else 6.0
                            except:
                                dur_clipe = 6.0

                            if dur_cena_audio <= dur_clipe:
                                # Áudio cabe no clipe — só cortar
                                clipe_final = os.path.join(job_dir, f"clipe_final_{ci+1:04d}.mp4")
                                cmd_cut = ["ffmpeg", "-y", "-i", cp, "-t", str(dur_cena_audio),
                                           "-c:v", "copy", "-an", clipe_final]
                                subprocess.run(cmd_cut, capture_output=True, text=True)
                                if os.path.exists(clipe_final):
                                    clipes_validos.append(clipe_final)
                                else:
                                    clipes_validos.append(cp)
                                sys.stderr.write(f"[SYNC] Cena {ci+1}: clipe cortado em {dur_cena_audio:.1f}s\n"); sys.stderr.flush()
                            else:
                                # Áudio maior que o clipe — animação + imagem estática pro restante
                                dur_restante = dur_cena_audio - dur_clipe
                                img = imagens[ci]
                                # Criar clipe estático pro restante
                                clipe_estatico = os.path.join(job_dir, f"clipe_static_{ci+1:04d}.mp4")
                                try:
                                    im = Image.open(img["path"])
                                    w, h = im.size
                                    im.close()
                                except:
                                    w, h = 1024, 1792
                                cmd_static = ["ffmpeg", "-y", "-loop", "1", "-t", str(dur_restante + 0.5),
                                              "-i", os.path.abspath(img["path"]),
                                              "-vf", f"scale={w}:{h},zoompan=z='1.08':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={int(dur_restante*25)}:s={w}x{h}:fps=25,trim=duration={dur_restante},setpts=PTS-STARTPTS",
                                              "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p", "-an", clipe_estatico]
                                subprocess.run(cmd_static, capture_output=True, text=True)
                                # Concatenar animação + estático
                                clipe_final = os.path.join(job_dir, f"clipe_final_{ci+1:04d}.mp4")
                                # Re-encode o clipe animado pro mesmo codec
                                clipe_anim_re = os.path.join(job_dir, f"clipe_anim_re_{ci+1:04d}.mp4")
                                cmd_re = ["ffmpeg", "-y", "-i", cp,
                                          "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p", "-an", clipe_anim_re]
                                subprocess.run(cmd_re, capture_output=True, text=True)
                                concat_cena = os.path.join(job_dir, f"concat_cena_{ci+1:04d}.txt")
                                with open(concat_cena, "w") as f:
                                    f.write(f"file '{os.path.abspath(clipe_anim_re)}'\n")
                                    if os.path.exists(clipe_estatico):
                                        f.write(f"file '{os.path.abspath(clipe_estatico)}'\n")
                                cmd_concat = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_cena,
                                              "-c:v", "copy", "-an", clipe_final]
                                subprocess.run(cmd_concat, capture_output=True, text=True)
                                if os.path.exists(clipe_final):
                                    clipes_validos.append(clipe_final)
                                else:
                                    clipes_validos.append(cp)
                                sys.stderr.write(f"[SYNC] Cena {ci+1}: animacao {dur_clipe:.1f}s + estatico {dur_restante:.1f}s = {dur_cena_audio:.1f}s\n"); sys.stderr.flush()

                    # PASSO 1: Concatenar clipes cortados
                    concat_path = os.path.join(job_dir, "concat_list.txt")
                    video_mudo = os.path.join(job_dir, "video_mudo.mp4")
                    with open(concat_path, "w") as f:
                        for cp in clipes_validos:
                            f.write(f"file '{os.path.abspath(cp)}'\n")
                    cmd_mudo = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_path, "-c:v", "copy", "-an", video_mudo]
                    subprocess.run(cmd_mudo, capture_output=True, text=True)

                    # PASSO 2: Medir durações reais
                    def medir_duracao(filepath):
                        try:
                            p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", filepath], capture_output=True, text=True)
                            return float(p.stdout.strip()) if p.returncode == 0 else 0
                        except: return 0

                    dur_video = medir_duracao(video_mudo)
                    dur_audio = medir_duracao(audio_final_path) if audio_final_path and os.path.exists(audio_final_path) else 0

                    sys.stderr.write(f"[SYNC] Video mudo: {dur_video:.1f}s | Audio: {dur_audio:.1f}s\n"); sys.stderr.flush()

                    video_path = os.path.join(OUTPUT_FOLDER, f"{job_id}.mp4")

                    # PASSO 3: Combinar vídeo + áudio
                    if audio_final_path and os.path.exists(audio_final_path) and dur_audio > 0:
                        if dur_video >= dur_audio:
                            # Vídeo mais longo ou igual ao áudio: cortar vídeo no fim do áudio
                            cmd_final = ["ffmpeg", "-y", "-i", video_mudo, "-i", audio_final_path,
                                         "-t", str(dur_audio),
                                         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", video_path]
                        else:
                            # Áudio mais longo que vídeo: loop o vídeo pra cobrir o áudio
                            cmd_final = ["ffmpeg", "-y",
                                         "-stream_loop", "-1", "-i", video_mudo,
                                         "-i", audio_final_path,
                                         "-t", str(dur_audio),
                                         "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
                                         "-c:a", "aac", "-b:a", "192k", video_path]
                            sys.stderr.write(f"[SYNC] Looping video pra cobrir audio ({dur_audio:.1f}s)\n"); sys.stderr.flush()
                    else:
                        # Sem áudio: usar vídeo mudo direto
                        shutil.copy(video_mudo, video_path)
                        cmd_final = None

                    if cmd_final:
                        result = subprocess.run(cmd_final, capture_output=True, text=True)
                        if result.returncode != 0:
                            sys.stderr.write(f"[SYNC] FFmpeg erro: {result.stderr[-300:]}\n"); sys.stderr.flush()
                            # Fallback: copiar vídeo mudo
                            shutil.copy(video_mudo, video_path)

                    # PASSO 4: Adicionar legendas se ativo
                    if legenda_cfg and legenda_cfg.get("ativo") and audio_final_path and os.path.exists(video_path):
                        srt_path = video_path.replace(".mp4", ".srt")
                        gerar_srt_palavras(audio_final_path, srt_path)
                        fonte = legenda_cfg.get("fonte", "Arial")
                        cor = legenda_cfg.get("cor", "&H00FFFFFF")
                        tam = legenda_cfg.get("tamanho", "18")
                        pos = legenda_cfg.get("posicao", "2")
                        sombra = "1" if legenda_cfg.get("sombra", True) else "0"
                        video_com_leg = video_path.replace(".mp4", "_leg.mp4")
                        srt_esc = os.path.abspath(srt_path)
                        cmd_sub = ["ffmpeg", "-y", "-i", video_path,
                                   "-vf", f"subtitles={srt_esc}:force_style='FontName={fonte},FontSize={tam},PrimaryColour={cor},Alignment={pos},Shadow={sombra},Bold=1'",
                                   "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
                                   "-c:a", "copy", video_com_leg]
                        result = subprocess.run(cmd_sub, capture_output=True, text=True)
                        if result.returncode == 0 and os.path.exists(video_com_leg):
                            os.replace(video_com_leg, video_path)
                else:
                    # Nenhum clipe gerado
                    jobs[job_id] = {"status": "erro", "progresso": "Estamos com problemas técnicos na animação. Por favor, tente novamente mais tarde. Se o erro persistir, entre em contato com o suporte técnico.", "total": 0, "atual": 0}
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
                        # Loop vídeo se áudio for mais longo
                        dur_concat = 0
                        try:
                            p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", concat_path.replace("concat_mix.txt","")], capture_output=True, text=True)
                        except: pass
                        cmd_concat += ["-i", audio_final_path, "-c:v", "copy", "-c:a", "aac", "-b:a", "192k"]
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
                    # Medir duração do vídeo pra cortar a música no tamanho certo
                    try:
                        p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                            "-of", "default=noprint_wrappers=1:nokey=1", video_path],
                                           capture_output=True, text=True)
                        dur_video_final = float(p.stdout.strip()) if p.returncode == 0 else 0
                    except:
                        dur_video_final = 0

                    # Mixar: narração em volume normal + música audível
                    if audio_final_path and os.path.exists(audio_final_path):
                        cmd = ["ffmpeg", "-y", "-i", video_path, "-i", musica_path,
                               "-filter_complex", "[1:a]volume=0.30[bg];[0:a][bg]amix=inputs=2:duration=first[aout]",
                               "-map", "0:v", "-map", "[aout]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", video_com_musica]
                    else:
                        # Sem narração: música como áudio principal (volume 70%), cortada na duração do vídeo
                        trim_filter = f"[1:a]volume=0.7,atrim=0:{dur_video_final},asetpts=PTS-STARTPTS[bg]" if dur_video_final > 0 else "[1:a]volume=0.7[bg]"
                        cmd = ["ffmpeg", "-y", "-i", video_path, "-i", musica_path,
                               "-filter_complex", trim_filter,
                               "-map", "0:v", "-map", "[bg]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                               "-shortest", video_com_musica]
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                    if result.returncode == 0 and os.path.exists(video_com_musica):
                        os.replace(video_com_musica, video_path)
                except Exception as e:
                    import sys
                    sys.stderr.write(f"[MUSICA] Erro ao mixar: {e}\n"); sys.stderr.flush()

            # Efeitos sonoros automáticos
            if efeitos_sonoros and os.path.exists(video_path):
                jobs[job_id]["progresso"] = "Analisando cenas para efeitos sonoros..."
                try:
                    import sys as _sys
                    # 1. Buscar efeitos disponíveis no banco
                    conn_sfx = sqlite3.connect('instance/veo3.db')
                    try: conn_sfx.execute("ALTER TABLE musicas_sistema ADD COLUMN tipo TEXT DEFAULT 'musica'")
                    except: pass
                    sfx_rows = conn_sfx.execute("SELECT id, nome, categoria, path FROM musicas_sistema WHERE tipo='efeito'").fetchall()
                    conn_sfx.close()
                    sfx_disponiveis = [{"id": r[0], "nome": r[1], "categoria": r[2], "path": r[3]} for r in sfx_rows
                                       if os.path.exists(os.path.join(MUSICAS_SISTEMA_FOLDER, r[3]))]

                    if sfx_disponiveis and imagens:
                        # 2. Pedir ao GPT pra analisar cada cena e sugerir efeitos
                        api_key = user.get_api_key()
                        if api_key:
                            categorias_sfx = list(set(cat.strip() for s in sfx_disponiveis for cat in s["categoria"].split(",")))
                            cenas_texto = []
                            for img in imagens:
                                cenas_texto.append(f"Cena {img['index']} [{img['inicio']}s-{img['fim']}s]: {img['texto']}")
                            prompt_sfx = f"""Analise estas cenas de um vídeo e sugira efeitos sonoros para dar mais impacto.

Cenas:
{chr(10).join(cenas_texto)}

Categorias de efeitos disponíveis: {', '.join(categorias_sfx)}

Para cada cena que merece um efeito, retorne no formato (uma por linha):
CENA:numero|MOMENTO:inicio/meio/fim|CATEGORIA:categoria

Regras:
- NÃO coloque efeito em todas as cenas, só onde faz sentido (ação, impacto, transição, emoção forte)
- Máximo 1 efeito por cena
- Se nenhuma cena precisa de efeito, retorne: NENHUM
- Use APENAS categorias da lista disponível"""

                            headers_sfx = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
                            body_sfx = {"model": "gpt-4o-mini", "messages": [
                                {"role": "system", "content": "Você é um sound designer de vídeos. Sugira efeitos sonoros pontuais para dar impacto nas cenas certas."},
                                {"role": "user", "content": prompt_sfx}
                            ], "max_tokens": 300}
                            r_sfx = requests.post("https://api.openai.com/v1/chat/completions", headers=headers_sfx, json=body_sfx, timeout=15)

                            if r_sfx.ok:
                                resposta_sfx = r_sfx.json()["choices"][0]["message"]["content"].strip()
                                _sys.stderr.write(f"[SFX] GPT sugeriu: {resposta_sfx}\n"); _sys.stderr.flush()

                                if "NENHUM" not in resposta_sfx.upper():
                                    # 3. Parsear sugestões
                                    sfx_aplicar = []
                                    for linha in resposta_sfx.split("\n"):
                                        linha = linha.strip()
                                        if not linha or "CENA:" not in linha.upper():
                                            continue
                                        try:
                                            partes = {}
                                            for p in linha.split("|"):
                                                if ":" in p:
                                                    k, v = p.split(":", 1)
                                                    partes[k.strip().upper()] = v.strip().lower()
                                            cena_num = int(partes.get("CENA", "0"))
                                            momento = partes.get("MOMENTO", "meio")
                                            cat_sfx = partes.get("CATEGORIA", "")
                                            if cena_num > 0 and cat_sfx:
                                                # Buscar efeito que bate com a categoria
                                                candidatos = [s for s in sfx_disponiveis if cat_sfx in s["categoria"].lower()]
                                                if candidatos:
                                                    import random
                                                    escolhido = random.choice(candidatos)
                                                    # Calcular timestamp
                                                    cena_img = next((im for im in imagens if im["index"] == cena_num), None)
                                                    if cena_img:
                                                        inicio_cena = cena_img["inicio"]
                                                        fim_cena = cena_img["fim"]
                                                        dur_cena = fim_cena - inicio_cena
                                                        if momento == "inicio":
                                                            ts = inicio_cena + 0.3
                                                        elif momento == "fim":
                                                            ts = max(inicio_cena, fim_cena - 1.0)
                                                        else:
                                                            ts = inicio_cena + dur_cena * 0.5
                                                        sfx_aplicar.append({
                                                            "timestamp": round(ts, 2),
                                                            "path": os.path.join(MUSICAS_SISTEMA_FOLDER, escolhido["path"]),
                                                            "nome": escolhido["nome"],
                                                            "cena": cena_num
                                                        })
                                        except:
                                            continue

                                    # 4. Mixar efeitos no vídeo com FFmpeg
                                    if sfx_aplicar:
                                        jobs[job_id]["progresso"] = f"Adicionando {len(sfx_aplicar)} efeito(s) sonoro(s)..."
                                        _sys.stderr.write(f"[SFX] Aplicando {len(sfx_aplicar)} efeitos\n"); _sys.stderr.flush()

                                        video_com_sfx = video_path.replace(".mp4", "_sfx.mp4")
                                        # Construir comando FFmpeg com múltiplos inputs
                                        cmd_sfx = ["ffmpeg", "-y", "-i", video_path]
                                        for sfx in sfx_aplicar:
                                            cmd_sfx += ["-i", sfx["path"]]

                                        # Construir filter_complex
                                        n_sfx = len(sfx_aplicar)
                                        filters = []
                                        for i, sfx in enumerate(sfx_aplicar):
                                            # Delay em ms, volume baixo pra não sobrepor narração
                                            delay_ms = int(sfx["timestamp"] * 1000)
                                            vol = "0.6" if audio_final_path else "0.8"
                                            filters.append(f"[{i+1}:a]adelay={delay_ms}|{delay_ms},volume={vol}[sfx{i}]")

                                        # Mixar tudo junto
                                        mix_inputs = "[0:a]" + "".join(f"[sfx{i}]" for i in range(n_sfx))
                                        filters.append(f"{mix_inputs}amix=inputs={n_sfx+1}:duration=first:dropout_transition=2[aout]")

                                        cmd_sfx += ["-filter_complex", ";".join(filters),
                                                    "-map", "0:v", "-map", "[aout]",
                                                    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                                                    video_com_sfx]

                                        result_sfx = subprocess.run(cmd_sfx, capture_output=True, text=True, timeout=120)
                                        if result_sfx.returncode == 0 and os.path.exists(video_com_sfx):
                                            os.replace(video_com_sfx, video_path)
                                            _sys.stderr.write(f"[SFX] Efeitos aplicados com sucesso\n"); _sys.stderr.flush()
                                        else:
                                            _sys.stderr.write(f"[SFX] FFmpeg erro: {result_sfx.stderr[-300:]}\n"); _sys.stderr.flush()
                except Exception as e:
                    import sys
                    sys.stderr.write(f"[SFX] Erro geral: {e}\n"); sys.stderr.flush()

            # Cobrar renderização/legenda se não gastou nada com outros serviços
            if creditos_gastos_video == 0:
                custo_render = 1
                custo_legenda = 1 if (legenda_cfg and legenda_cfg.get("ativo")) else 0
                custo_total = custo_render + custo_legenda
                if not user.gastar_creditos(custo_total):
                    jobs[job_id] = {"status": "erro", "progresso": f"Créditos insuficientes. Necessário: {custo_total}", "total": 0, "atual": 0}
                    return
                db.session.commit()

            jobs[job_id]["progresso"] = "Compactando..."
            # Marca d'água no vídeo final (exceto api_propria, business e admin)
            if not usuario_sem_marca(user) and os.path.exists(video_path):
                video_wm = video_path.replace(".mp4", "_wm.mp4")
                wm_filter = get_marca_dagua_ffmpeg()
                cmd_wm = ["ffmpeg", "-y", "-i", video_path, "-vf", wm_filter,
                           "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
                           "-c:a", "copy", video_wm]
                result_wm = subprocess.run(cmd_wm, capture_output=True, text=True)
                if result_wm.returncode == 0 and os.path.exists(video_wm):
                    os.replace(video_wm, video_path)

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
            # Remover rascunho (storyboard) após finalizar vídeo
            try:
                sb_dir_final = os.path.join(STORYBOARD_FOLDER, sb_id)
                if os.path.exists(sb_dir_final):
                    shutil.rmtree(sb_dir_final)
            except: pass
            jobs[job_id] = {"status": "pronto", "progresso": "Concluido", "total": len(imagens), "atual": len(imagens), "zip": zip_path, "video": video_path}
        except Exception as e:
            jobs[job_id] = {"status": "erro", "progresso": str(e), "total": 0, "atual": 0}

# ── Rotas Auth ────────────────────────────────────────────
@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    # Contar visita na landing
    try:
        conn = sqlite3.connect('instance/veo3.db')
        conn.execute("CREATE TABLE IF NOT EXISTS visitas (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT, ip TEXT, criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        from datetime import date
        conn.execute("INSERT INTO visitas (data, ip) VALUES (?, ?)", (date.today().isoformat(), get_real_ip()))
        conn.commit()
        conn.close()
    except:
        pass
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
        # Rate limit: max 5 tentativas por IP por minuto
        ip = get_real_ip()
        if rate_limit_check(f"login_{ip}", max_requests=5, window=60):
            return jsonify({"erro": "Muitas tentativas. Aguarde 1 minuto."}), 429
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
        # Rate limit: max 3 cadastros por IP por minuto
        ip = get_real_ip()
        if rate_limit_check(f"cadastro_{ip}", max_requests=3, window=60):
            return jsonify({"erro": "Muitas tentativas. Aguarde 1 minuto."}), 429
        data = request.json
        email = data.get("email", "").strip().lower()
        nome = data.get("nome", "").strip()
        senha = data.get("senha", "")
        # Validação básica de email
        if not email or "@" not in email or "." not in email.split("@")[-1]:
            return jsonify({"erro": "Email inválido"}), 400
        if not nome or len(nome) < 2:
            return jsonify({"erro": "Nome inválido"}), 400
        if not senha or len(senha) < 6:
            return jsonify({"erro": "Senha deve ter pelo menos 6 caracteres"}), 400
        if User.query.filter_by(email=email).first():
            return jsonify({"erro": "Email ja cadastrado"}), 400
        user = User(email=email, nome=nome, senha=generate_password_hash(senha))
        db.session.add(user)
        db.session.commit()
        # Email de boas-vindas
        enviar_email(user.email, "Bem-vindo ao Klyonclaw Studio! 🎬", f"""
        <div style="font-family:Arial;max-width:500px;margin:0 auto;padding:20px">
            <h1 style="color:#4a9eff">Klyonclaw Studio</h1>
            <p>Olá <b>{user.nome}</b>! 👋</p>
            <p>Sua conta foi criada com sucesso. Agora você pode criar vídeos incríveis com inteligência artificial.</p>
            <p>Escolha um plano e comece a criar:</p>
            <a href="https://studio.klyonclaw.com/dashboard" style="display:inline-block;padding:12px 24px;background:#2563eb;color:#fff;border-radius:8px;text-decoration:none;font-weight:600">Acessar Klyonclaw Studio</a>
            <p style="color:#888;font-size:12px;margin-top:20px">Klyonclaw Studio — AI Video Automation</p>
        </div>""")
        login_user(user)
        return jsonify({"ok": True})
    return render_template("cadastro.html")

@app.route("/esqueci_senha", methods=["POST"])
def esqueci_senha():
    data = request.json
    email = data.get("email", "").strip()
    # Rate limit: max 2 por IP a cada 5 minutos
    ip = get_real_ip()
    if rate_limit_check(f"senha_{ip}", max_requests=2, window=300):
        return jsonify({"erro": "Muitas tentativas. Aguarde 5 minutos."}), 429
    user = User.query.filter_by(email=email).first()
    if not user:
        return jsonify({"erro": "Email nao encontrado"}), 404
    nova_senha = uuid.uuid4().hex[:8]
    user.senha = generate_password_hash(nova_senha)
    db.session.commit()
    # Enviar nova senha por email
    enviar_email(email, "Sua nova senha — Klyonclaw Studio", f"""
    <div style="font-family:Arial;max-width:500px;margin:0 auto;padding:20px">
        <h1 style="color:#4a9eff">Klyonclaw Studio</h1>
        <p>Olá <b>{user.nome}</b>,</p>
        <p>Sua senha foi redefinida. Use a nova senha abaixo para acessar sua conta:</p>
        <div style="background:#1a2332;border-radius:8px;padding:16px;text-align:center;margin:16px 0">
            <span style="font-size:1.4rem;font-weight:700;color:#4a9eff;letter-spacing:2px">{nova_senha}</span>
        </div>
        <p style="font-size:.85rem;color:#666">Recomendamos alterar sua senha após o login.</p>
        <a href="https://studio.klyonclaw.com/login" style="display:inline-block;padding:12px 24px;background:#2563eb;color:#fff;border-radius:8px;text-decoration:none;font-weight:600">Acessar minha conta</a>
        <p style="color:#888;font-size:12px;margin-top:20px">Se você não solicitou essa alteração, entre em contato conosco.</p>
    </div>""")
    return jsonify({"ok": True, "msg": "Nova senha enviada para seu email"})

# ── Thumbnail Engine ──────────────────────────────────────
THUMB_ESTILOS_ENGINE = {
    "cinematic": {"nome": "Cinematic", "prompt": "Cinematic film still, 35mm lens, shallow depth of field, dramatic rim lighting, cinematic color grading, anamorphic lens flare, movie poster composition, 4K, 16:9"},
    "hyper_realistic": {"nome": "Hyper Realistic", "prompt": "Hyper-realistic photograph, ultra-detailed skin textures, studio lighting, DSLR quality, sharp focus, photojournalistic style, natural colors, 4K, 16:9"},
    "mystery": {"nome": "Mystery", "prompt": "Dark mysterious atmosphere, fog and shadows, silhouette lighting, desaturated tones with single color accent, noir aesthetic, suspenseful mood, 4K, 16:9"},
    "documentary": {"nome": "Documentary", "prompt": "Documentary photography style, natural lighting, candid moment captured, editorial composition, muted earth tones, authentic and raw, 4K, 16:9"},
    "cartoon": {"nome": "Cartoon", "prompt": "Vibrant cartoon illustration, bold outlines, flat bright colors, exaggerated expressions, clean vector style, fun and energetic, 4K, 16:9"},
    "kids": {"nome": "Kids", "prompt": "Colorful children illustration, cute rounded characters, pastel rainbow colors, friendly and magical atmosphere, Pixar-inspired, 4K, 16:9"},
    "dark_drama": {"nome": "Dark Drama", "prompt": "Dark dramatic scene, chiaroscuro lighting, deep blacks and golden highlights, intense emotion, Renaissance painting meets cinema, 4K, 16:9"},
    "scifi": {"nome": "Sci-Fi", "prompt": "Futuristic sci-fi scene, neon holographic elements, cyberpunk lighting, chrome and glass surfaces, volumetric fog, advanced technology, 4K, 16:9"},
    "bright_viral": {"nome": "Bright Viral", "prompt": "Ultra-bright saturated colors, clean white or gradient background, bold pop-art influence, maximum visual impact, YouTube viral aesthetic, 4K, 16:9"},
    "minimal": {"nome": "Minimal", "prompt": "Minimalist composition, single subject centered, vast negative space, limited color palette, clean and elegant, modern design aesthetic, 4K, 16:9"},
}

THUMB_FOLDER = "thumbnails"
os.makedirs(THUMB_FOLDER, exist_ok=True)

CREDITOS_POR_THUMB_PACK = 24

def analisar_cena_thumbnail(roteiro, api_key):
    try:
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        system = """You are a YouTube thumbnail strategist. Analyze this video script and identify the SINGLE MOST CLICKABLE moment for a thumbnail.

Return a JSON object with:
{"cena":"scene description","objeto_central":"main object","personagem":"character description or null","emocao":"dominant emotion","tensao_visual":"visual tension","cenario":"setting","acao_visual":"action","estilo_recomendado":"cinematic","textos_sugeridos":["TEXT1","TEXT2","TEXT3","TEXT4","TEXT5"]}

RULES for textos_sugeridos: 2-4 words MAX each, same language as script, high impact, provocative or mysterious. Output ONLY valid JSON."""
        body = {"model": "gpt-4o-mini", "messages": [{"role": "system", "content": system}, {"role": "user", "content": roteiro}], "max_tokens": 500}
        r = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=body, timeout=30)
        if r.ok:
            texto = r.json()["choices"][0]["message"]["content"].strip().replace("```json", "").replace("```", "").strip()
            return json.loads(texto)
    except: pass
    return None

def gerar_prompt_thumb_engine(analise, estilo, variacao=0, texto_thumb="", sem_texto=False):
    estilo_info = THUMB_ESTILOS_ENGINE.get(estilo, THUMB_ESTILOS_ENGINE["cinematic"])
    cena = analise.get("cena", "dramatic scene")
    personagem = analise.get("personagem", "")
    objeto = analise.get("objeto_central", "")
    emocao = analise.get("emocao", "intense")
    cenario = analise.get("cenario", "")
    acao = analise.get("acao_visual", "")
    composicoes = [
        "extreme close-up on the main subject, shallow depth of field, blurred background",
        "medium shot, rule of thirds composition, subject slightly off-center",
        "dramatic low angle looking up at the subject, imposing perspective",
        "wide establishing shot showing scale, subject small but highlighted",
    ]
    comp = composicoes[variacao % len(composicoes)]
    if sem_texto:
        texto_instrucao = "ABSOLUTELY NO text, letters, words, or writing in the image."
    elif texto_thumb:
        texto_instrucao = f'Bold modern sans-serif text "{texto_thumb}" in huge thick white letters with strong black drop shadow, occupying 30% of image area.'
    else:
        textos = analise.get("textos_sugeridos", [])
        if textos and variacao < len(textos):
            texto_instrucao = f'Bold modern sans-serif text "{textos[variacao]}" in huge thick white letters with strong black drop shadow, occupying 30% of image area.'
        else:
            texto_instrucao = "ABSOLUTELY NO text in the image."
    subject = personagem if personagem else objeto
    return f"{estilo_info['prompt']}. {comp}. Scene: {cena}. Main subject: {subject} showing {emocao} emotion. {acao}. Setting: {cenario}. {texto_instrucao}. High contrast, sharp focus, clean background, ultra-detailed, professional YouTube thumbnail, maximum visual impact."[:800]

def _init_thumbnails_table():
    try:
        conn = sqlite3.connect('instance/veo3.db')
        conn.execute("""CREATE TABLE IF NOT EXISTS thumbnails (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, thumb_id TEXT NOT NULL, session_id TEXT DEFAULT '', roteiro TEXT, prompt TEXT, estilo TEXT, analise TEXT DEFAULT '', texto_aplicado TEXT DEFAULT '', path TEXT, criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        try: conn.execute("ALTER TABLE thumbnails ADD COLUMN session_id TEXT DEFAULT ''")
        except: pass
        try: conn.execute("ALTER TABLE thumbnails ADD COLUMN analise TEXT DEFAULT ''")
        except: pass
        try: conn.execute("ALTER TABLE thumbnails ADD COLUMN texto_aplicado TEXT DEFAULT ''")
        except: pass
        conn.commit(); conn.close()
    except: pass

@app.route("/thumb_engine/analisar", methods=["POST"])
@login_required
def thumb_engine_analisar():
    roteiro = request.json.get("roteiro", "").strip()
    if not roteiro: return jsonify({"erro": "Roteiro vazio"}), 400
    if not current_user.get_api_key(): return jsonify({"erro": "Assine um plano para usar."}), 400
    if not current_user.gastar_creditos(1): return jsonify({"erro": "Créditos insuficientes."}), 400
    db.session.commit()
    analise = analisar_cena_thumbnail(roteiro, current_user.get_api_key())
    if not analise:
        current_user.creditos += 1; db.session.commit()
        return jsonify({"erro": "Erro ao analisar roteiro"}), 500
    return jsonify({"ok": True, "analise": analise})

@app.route("/thumb_engine/gerar", methods=["POST"])
@login_required
def thumb_engine_gerar():
    if rate_limit_check(f"thumbgen_{current_user.id}", max_requests=3, window=60):
        return jsonify({"erro": "Aguarde antes de gerar novamente."}), 429
    roteiro = request.form.get("roteiro", "").strip()
    estilo = request.form.get("estilo", "cinematic").strip()
    analise_json = request.form.get("analise", "{}").strip()
    texto_thumb = request.form.get("texto_thumb", "").strip()
    sem_texto = request.form.get("sem_texto", "false") == "true"
    if not current_user.get_api_key(): return jsonify({"erro": "Assine um plano."}), 400
    if not current_user.gastar_creditos(CREDITOS_POR_THUMB_PACK):
        return jsonify({"erro": f"Créditos insuficientes. Necessário: {CREDITOS_POR_THUMB_PACK}"}), 400
    db.session.commit()
    try: analise = json.loads(analise_json)
    except: analise = {"cena": roteiro[:200], "textos_sugeridos": []}
    session_id = uuid.uuid4().hex[:12]
    api_key = current_user.get_api_key()
    _init_thumbnails_table()
    import sys
    def gerar_variacao(i):
        try:
            prompt = gerar_prompt_thumb_engine(analise, estilo, i, texto_thumb, sem_texto)
            thumb_id = uuid.uuid4().hex[:12]
            thumb_path = os.path.join(THUMB_FOLDER, f"{current_user.id}_{thumb_id}.png")
            gerar_imagem_openai(prompt, api_key, "1792x1024", "standard", thumb_path, modelo="gpt-image-1")
            try:
                from PIL import ImageEnhance, ImageFilter
                img = Image.open(thumb_path); img = img.filter(ImageFilter.SHARPEN)
                img = ImageEnhance.Contrast(img).enhance(1.1); img = ImageEnhance.Brightness(img).enhance(1.05)
                img.save(thumb_path, quality=95); img.close()
            except: pass
            conn = sqlite3.connect('instance/veo3.db')
            conn.execute("INSERT INTO thumbnails (user_id, thumb_id, session_id, roteiro, prompt, estilo, analise, texto_aplicado, path) VALUES (?,?,?,?,?,?,?,?,?)",
                         (current_user.id, thumb_id, session_id, roteiro[:500], prompt, estilo, analise_json[:1000], texto_thumb, thumb_path))
            conn.commit(); conn.close()
            texto_na = texto_thumb if texto_thumb else (analise.get("textos_sugeridos", [])[i] if i < len(analise.get("textos_sugeridos", [])) else "")
            return {"thumb_id": thumb_id, "prompt": prompt, "variacao": i + 1, "texto": texto_na}
        except Exception as e:
            sys.stderr.write(f"[THUMB] Var {i+1} erro: {e}\n"); sys.stderr.flush(); return None
    with ThreadPoolExecutor(max_workers=2) as executor:
        resultados = list(executor.map(gerar_variacao, range(4)))
    thumbs_geradas = [r for r in resultados if r]
    if not thumbs_geradas:
        current_user.creditos += CREDITOS_POR_THUMB_PACK; db.session.commit()
        return jsonify({"erro": "Erro ao gerar thumbnails."}), 500
    falhas = 4 - len(thumbs_geradas)
    if falhas > 0: current_user.creditos += falhas * 6; db.session.commit()
    return jsonify({"ok": True, "session_id": session_id, "thumbs": thumbs_geradas, "analise": analise, "textos_sugeridos": analise.get("textos_sugeridos", [])})

@app.route("/thumb_engine/regerar_uma", methods=["POST"])
@login_required
def thumb_engine_regerar_uma():
    roteiro = request.form.get("roteiro", "").strip()
    estilo = request.form.get("estilo", "cinematic").strip()
    analise_json = request.form.get("analise", "{}").strip()
    texto_thumb = request.form.get("texto_thumb", "").strip()
    sem_texto = request.form.get("sem_texto", "false") == "true"
    if not current_user.get_api_key(): return jsonify({"erro": "Sem API"}), 400
    if not current_user.gastar_creditos(CREDITOS_POR_IMAGEM): return jsonify({"erro": "Créditos insuficientes."}), 400
    db.session.commit()
    try: analise = json.loads(analise_json)
    except: analise = {"cena": roteiro[:200]}
    import random
    prompt = gerar_prompt_thumb_engine(analise, estilo, random.randint(0, 3), texto_thumb, sem_texto)
    thumb_id = uuid.uuid4().hex[:12]
    thumb_path = os.path.join(THUMB_FOLDER, f"{current_user.id}_{thumb_id}.png")
    try:
        gerar_imagem_openai(prompt, current_user.get_api_key(), "1792x1024", "standard", thumb_path, modelo="gpt-image-1")
        try:
            from PIL import ImageEnhance, ImageFilter
            img = Image.open(thumb_path); img = img.filter(ImageFilter.SHARPEN)
            img = ImageEnhance.Contrast(img).enhance(1.1); img.save(thumb_path, quality=95); img.close()
        except: pass
        _init_thumbnails_table()
        conn = sqlite3.connect('instance/veo3.db')
        conn.execute("INSERT INTO thumbnails (user_id, thumb_id, roteiro, prompt, estilo, analise, texto_aplicado, path) VALUES (?,?,?,?,?,?,?,?)",
                     (current_user.id, thumb_id, roteiro[:500], prompt, estilo, analise_json[:1000], texto_thumb, thumb_path))
        conn.commit(); conn.close()
        return jsonify({"ok": True, "thumb_id": thumb_id, "prompt": prompt})
    except Exception as e:
        current_user.creditos += CREDITOS_POR_IMAGEM; db.session.commit()
        return jsonify({"erro": str(e)}), 500

@app.route("/minhas_thumbnails")
@login_required
def minhas_thumbnails():
    _init_thumbnails_table()
    conn = sqlite3.connect('instance/veo3.db')
    rows = conn.execute("SELECT thumb_id, roteiro, prompt, estilo, criado_em FROM thumbnails WHERE user_id=? ORDER BY id DESC LIMIT 50", (current_user.id,)).fetchall()
    conn.close()
    thumbs = [{"thumb_id": r[0], "roteiro": r[1] or "", "prompt": r[2] or "", "estilo": r[3] or "cinematic", "criado_em": r[4] or ""} for r in rows if os.path.exists(os.path.join(THUMB_FOLDER, f"{current_user.id}_{r[0]}.png"))]
    return jsonify({"thumbnails": thumbs})

@app.route("/deletar_thumbnail", methods=["POST"])
@login_required
def deletar_thumbnail():
    thumb_id = request.json.get("thumb_id", "")
    path = os.path.join(THUMB_FOLDER, f"{current_user.id}_{thumb_id}.png")
    if os.path.exists(path): os.remove(path)
    conn = sqlite3.connect('instance/veo3.db')
    conn.execute("DELETE FROM thumbnails WHERE user_id=? AND thumb_id=?", (current_user.id, thumb_id))
    conn.commit(); conn.close()
    return jsonify({"ok": True})

@app.route("/download_thumbnail/<thumb_id>")
@login_required
def download_thumbnail(thumb_id):
    thumb_path = os.path.join(THUMB_FOLDER, f"{current_user.id}_{thumb_id}.png")
    if not os.path.exists(thumb_path): return jsonify({"erro": "Não encontrada"}), 404
    return send_file(thumb_path, as_attachment=True, download_name=f"thumbnail_{thumb_id}.png")

@app.route("/ver_thumbnail/<thumb_id>")
@login_required
def ver_thumbnail(thumb_id):
    thumb_path = os.path.join(THUMB_FOLDER, f"{current_user.id}_{thumb_id}.png")
    if not os.path.exists(thumb_path): return jsonify({"erro": "Não encontrada"}), 404
    return send_file(thumb_path)

# ── Thumbnail Editor (IA) ─────────────────────────────────
THUMB_EDIT_FOLDER = "thumb_edits"
os.makedirs(THUMB_EDIT_FOLDER, exist_ok=True)

def _init_thumb_edits_table():
    try:
        conn = sqlite3.connect('instance/veo3.db')
        conn.execute("""CREATE TABLE IF NOT EXISTS thumb_edits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            edit_id TEXT NOT NULL,
            original_path TEXT,
            rosto_path TEXT DEFAULT '',
            instrucao TEXT,
            resultado_path TEXT,
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.commit(); conn.close()
    except: pass

def editar_imagem_openai(prompt, api_key, imagem_paths, output_path, size="1792x1024"):
    """Edita imagem usando gpt-image-1 com imagens de referência via /v1/images/edits"""
    import base64, sys
    headers = {"Authorization": f"Bearer {api_key}"}

    size_map = {"1792x1024": "1536x1024", "1024x1792": "1024x1536", "1024x1024": "1024x1024"}
    gpt_size = size_map.get(size, "1536x1024")

    # Usar multipart form pra enviar imagens
    files = []
    for i, img_path in enumerate(imagem_paths):
        fname = os.path.basename(img_path)
        files.append(("image[]", (fname, open(img_path, "rb"), "image/png")))

    data = {
        "model": "gpt-image-1",
        "prompt": prompt,
        "size": gpt_size,
        "quality": "medium",
        "n": "1"
    }

    sys.stderr.write(f"[THUMB-EDIT] Editando com {len(imagem_paths)} imagem(ns)...\n"); sys.stderr.flush()
    r = requests.post("https://api.openai.com/v1/images/edits", headers=headers, data=data, files=files, timeout=120)

    # Fechar arquivos
    for _, f in files:
        f[1].close()

    if r.ok:
        resp_data = r.json()
        img_bytes = base64.b64decode(resp_data["data"][0]["b64_json"])
        with open(output_path, "wb") as f:
            f.write(img_bytes)
        # Pós-processamento
        try:
            from PIL import ImageEnhance, ImageFilter
            img = Image.open(output_path); img = img.filter(ImageFilter.SHARPEN)
            img = ImageEnhance.Contrast(img).enhance(1.1); img = ImageEnhance.Brightness(img).enhance(1.05)
            img.save(output_path, quality=95); img.close()
        except: pass
        sys.stderr.write(f"[THUMB-EDIT] OK\n"); sys.stderr.flush()
        return True
    else:
        erro = r.json().get("error", {}).get("message", r.text[:200])
        sys.stderr.write(f"[THUMB-EDIT] Erro: {erro}\n"); sys.stderr.flush()
        raise Exception(f"Erro na edição: {erro}")

@app.route("/thumb_editor/editar", methods=["POST"])
@login_required
def thumb_editor_editar():
    """Edita thumbnail existente com instrução de texto + opcionalmente rosto — gera 4 variações"""
    instrucao = request.form.get("instrucao", "").strip()
    if not instrucao:
        return jsonify({"erro": "Escreva uma instrução de edição"}), 400
    if "imagem" not in request.files or not request.files["imagem"].filename:
        return jsonify({"erro": "Envie a imagem de referência"}), 400
    # Validar tamanho (max 20MB)
    img_file = request.files["imagem"]
    img_file.seek(0, 2)
    if img_file.tell() > 20 * 1024 * 1024:
        return jsonify({"erro": "Imagem muito grande. Máximo 20MB."}), 400
    img_file.seek(0)
    if not current_user.get_api_key():
        return jsonify({"erro": "Assine um plano para usar."}), 400
    if not current_user.gastar_creditos(CREDITOS_POR_IMAGEM):
        return jsonify({"erro": "Créditos insuficientes."}), 400
    db.session.commit()

    edit_id = uuid.uuid4().hex[:12]
    original_path = os.path.join(THUMB_EDIT_FOLDER, f"{current_user.id}_{edit_id}_orig.png")
    request.files["imagem"].save(original_path)

    rosto_path = ""
    imagem_paths = [original_path]
    if "rosto" in request.files and request.files["rosto"].filename:
        rosto_path = os.path.join(THUMB_EDIT_FOLDER, f"{current_user.id}_{edit_id}_rosto.png")
        request.files["rosto"].save(rosto_path)
        imagem_paths.append(rosto_path)

    prompt = f"""Edit this thumbnail image following these instructions: {instrucao}

CRITICAL RULES:
- PRESERVE the original composition, framing, and 16:9 aspect ratio
- PRESERVE the position of main elements
- Apply ONLY the changes requested
- Keep the image looking like a professional YouTube thumbnail
- High contrast, sharp focus, vibrant colors"""
    if rosto_path:
        prompt += "\n- The second image is a face/person reference. Replace the person in the thumbnail with this face/person while keeping the same pose and position."

    resultado_path = os.path.join(THUMB_FOLDER, f"{current_user.id}_{edit_id}.png")

    try:
        editar_imagem_openai(prompt, current_user.get_api_key(), imagem_paths, resultado_path)
        _init_thumb_edits_table()
        conn = sqlite3.connect('instance/veo3.db')
        conn.execute("INSERT INTO thumb_edits (user_id, edit_id, original_path, rosto_path, instrucao, resultado_path) VALUES (?,?,?,?,?,?)",
                     (current_user.id, edit_id, original_path, rosto_path, instrucao, resultado_path))
        conn.commit(); conn.close()
        _init_thumbnails_table()
        conn = sqlite3.connect('instance/veo3.db')
        conn.execute("INSERT INTO thumbnails (user_id, thumb_id, roteiro, prompt, estilo, path) VALUES (?,?,?,?,?,?)",
                     (current_user.id, edit_id, instrucao[:500], prompt[:500], "editado", resultado_path))
        conn.commit(); conn.close()
        return jsonify({"ok": True, "edit_id": edit_id})
    except Exception as e:
        current_user.creditos += CREDITOS_POR_IMAGEM; db.session.commit()
        return jsonify({"erro": str(e)}), 500

@app.route("/thumb_editor/viral_tuning", methods=["POST"])
@login_required
def thumb_editor_viral_tuning():
    """Aplica Viral Tuning automático numa thumbnail"""
    if "imagem" not in request.files or not request.files["imagem"].filename:
        return jsonify({"erro": "Envie a imagem"}), 400
    if not current_user.get_api_key():
        return jsonify({"erro": "Assine um plano."}), 400
    if not current_user.gastar_creditos(CREDITOS_POR_IMAGEM):
        return jsonify({"erro": "Créditos insuficientes."}), 400
    db.session.commit()

    edit_id = uuid.uuid4().hex[:12]
    original_path = os.path.join(THUMB_EDIT_FOLDER, f"{current_user.id}_{edit_id}_viral_orig.png")
    request.files["imagem"].save(original_path)

    prompt = """Enhance this thumbnail for MAXIMUM viral potential and click-through rate. Apply ALL of these improvements:
- INCREASE contrast dramatically
- SHARPEN all details, especially faces and text
- Make the background LESS cluttered and more blurred
- Make the main subject POP with brighter lighting and stronger colors
- Add subtle rim lighting or glow around the main subject
- Make any text BOLDER and more readable with stronger shadow/outline
- Increase color saturation for more visual impact
- Slightly zoom into the main subject for more intimacy
- Clean up any visual noise or distracting elements
- Make it look like a TOP-PERFORMING YouTube thumbnail
- PRESERVE the original composition and layout
- Output must be a polished, professional, viral-ready thumbnail"""

    resultado_path = os.path.join(THUMB_FOLDER, f"{current_user.id}_{edit_id}.png")

    try:
        editar_imagem_openai(prompt, current_user.get_api_key(), [original_path], resultado_path)
        _init_thumbnails_table()
        conn = sqlite3.connect('instance/veo3.db')
        conn.execute("INSERT INTO thumbnails (user_id, thumb_id, roteiro, prompt, estilo, path) VALUES (?,?,?,?,?,?)",
                     (current_user.id, edit_id, "Viral Tuning", prompt[:500], "viral", resultado_path))
        conn.commit(); conn.close()
        return jsonify({"ok": True, "edit_id": edit_id})
    except Exception as e:
        current_user.creditos += CREDITOS_POR_IMAGEM; db.session.commit()
        return jsonify({"erro": str(e)}), 500

@app.route("/thumb_editor/ranking", methods=["POST"])
@login_required
def thumb_editor_ranking():
    """Analisa thumbnails e dá score de potencial de clique"""
    thumb_ids = request.json.get("thumb_ids", [])
    if not thumb_ids:
        return jsonify({"erro": "Nenhuma thumbnail selecionada"}), 400
    if not current_user.get_api_key():
        return jsonify({"erro": "Assine um plano."}), 400
    if not current_user.gastar_creditos(1):
        return jsonify({"erro": "Créditos insuficientes."}), 400
    db.session.commit()

    import base64
    images_data = []
    for tid in thumb_ids[:8]:
        path = os.path.join(THUMB_FOLDER, f"{current_user.id}_{tid}.png")
        if os.path.exists(path):
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            images_data.append({"id": tid, "b64": b64})

    if not images_data:
        return jsonify({"erro": "Nenhuma thumbnail encontrada"}), 404

    try:
        headers = {"Authorization": f"Bearer {current_user.get_api_key()}", "Content-Type": "application/json"}
        messages = [{"role": "system", "content": """You are a YouTube thumbnail CTR expert. Analyze each thumbnail and score it from 0-100 based on click potential.

Evaluate: contrast, visual clarity, subject focus, face/character size, visible emotion, text readability, background separation, element clutter.

Return ONLY valid JSON array:
[{"index":0,"score":85,"reason":"Strong contrast, clear subject, readable text"},{"index":1,"score":72,"reason":"Good composition but text too small"}]"""}]

        content = []
        for i, img in enumerate(images_data):
            content.append({"type": "text", "text": f"Thumbnail {i+1}:"})
            content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img['b64']}"}})
        messages.append({"role": "user", "content": content})

        body = {"model": "gpt-4o-mini", "messages": messages, "max_tokens": 500}
        r = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=body, timeout=30)
        if r.ok:
            texto = r.json()["choices"][0]["message"]["content"].strip().replace("```json", "").replace("```", "").strip()
            rankings = json.loads(texto)
            for rank in rankings:
                idx = rank.get("index", 0)
                if idx < len(images_data):
                    rank["thumb_id"] = images_data[idx]["id"]
            rankings.sort(key=lambda x: x.get("score", 0), reverse=True)
            return jsonify({"ok": True, "rankings": rankings})
    except Exception as e:
        return jsonify({"erro": str(e)}), 500
    return jsonify({"erro": "Erro ao analisar"}), 500

@app.route("/thumb_editor/ver/<edit_id>")
@login_required
def thumb_editor_ver(edit_id):
    path = os.path.join(THUMB_FOLDER, f"{current_user.id}_{edit_id}.png")
    if not os.path.exists(path): return jsonify({"erro": "Não encontrada"}), 404
    return send_file(path)

@app.route("/thumb_editor/visual")
@login_required
def thumb_editor_visual():
    return render_template("thumb_editor.html")

@app.route("/thumb_editor/save_project", methods=["POST"])
@login_required
def thumb_editor_save_project():
    """Salva projeto do editor visual em JSON"""
    data = request.json
    project_json = data.get("project", {})
    preview_b64 = data.get("preview", "")
    if not project_json:
        return jsonify({"erro": "Projeto vazio"}), 400
    # Limitar tamanho do projeto (max 5MB)
    project_str = json.dumps(project_json)
    if len(project_str) > 5 * 1024 * 1024:
        return jsonify({"erro": "Projeto muito grande"}), 400
    import base64
    project_id = uuid.uuid4().hex[:12]
    # Salvar JSON do projeto
    project_path = os.path.join(THUMB_FOLDER, f"{current_user.id}_proj_{project_id}.json")
    with open(project_path, "w") as f:
        json.dump(project_json, f)
    # Salvar preview PNG
    preview_path = ""
    if preview_b64 and "base64," in preview_b64:
        img_data = base64.b64decode(preview_b64.split("base64,")[1])
        preview_path = os.path.join(THUMB_FOLDER, f"{current_user.id}_{project_id}.png")
        with open(preview_path, "wb") as f:
            f.write(img_data)
        # Salvar no histórico de thumbnails
        _init_thumbnails_table()
        conn = sqlite3.connect('instance/veo3.db')
        conn.execute("INSERT INTO thumbnails (user_id, thumb_id, roteiro, prompt, estilo, path) VALUES (?,?,?,?,?,?)",
                     (current_user.id, project_id, "Editor Visual", "projeto", "editor", preview_path))
        conn.commit(); conn.close()
    # Salvar referência do projeto
    try:
        conn = sqlite3.connect('instance/veo3.db')
        conn.execute("""CREATE TABLE IF NOT EXISTS thumb_projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            project_id TEXT NOT NULL, project_path TEXT, preview_path TEXT,
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP, atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        conn.execute("INSERT INTO thumb_projects (user_id, project_id, project_path, preview_path) VALUES (?,?,?,?)",
                     (current_user.id, project_id, project_path, preview_path))
        conn.commit(); conn.close()
    except: pass
    return jsonify({"ok": True, "project_id": project_id})

@app.route("/thumb_editor/load_project/<project_id>")
@login_required
def thumb_editor_load_project(project_id):
    """Carrega projeto do editor visual"""
    project_path = os.path.join(THUMB_FOLDER, f"{current_user.id}_proj_{project_id}.json")
    if not os.path.exists(project_path):
        return jsonify({"erro": "Projeto não encontrado"}), 404
    with open(project_path) as f:
        project = json.load(f)
    return jsonify({"ok": True, "project": project})

@app.route("/thumb_editor/meus_projetos")
@login_required
def thumb_editor_meus_projetos():
    """Lista projetos salvos do editor visual"""
    try:
        conn = sqlite3.connect('instance/veo3.db')
        conn.execute("""CREATE TABLE IF NOT EXISTS thumb_projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            project_id TEXT NOT NULL, project_path TEXT, preview_path TEXT,
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP, atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        rows = conn.execute("SELECT project_id, criado_em FROM thumb_projects WHERE user_id=? ORDER BY id DESC LIMIT 20", (current_user.id,)).fetchall()
        conn.close()
        projetos = [{"project_id": r[0], "criado_em": r[1] or ""} for r in rows if os.path.exists(os.path.join(THUMB_FOLDER, f"{current_user.id}_{r[0]}.png"))]
        return jsonify({"projetos": projetos})
    except:
        return jsonify({"projetos": []})

@app.route("/thumb_editor/download/<edit_id>")
@login_required
def thumb_editor_download(edit_id):
    path = os.path.join(THUMB_FOLDER, f"{current_user.id}_{edit_id}.png")
    if not os.path.exists(path): return jsonify({"erro": "Não encontrada"}), 404
    return send_file(path, as_attachment=True, download_name=f"thumb_editada_{edit_id}.png")

@app.route("/thumb_editor/salvar_biblioteca", methods=["POST"])
@login_required
def thumb_editor_salvar_biblioteca():
    """Salva thumbnail do editor na biblioteca do banco de imagens"""
    if "imagem" not in request.files:
        return jsonify({"erro": "Envie a imagem"}), 400
    img_file = request.files["imagem"]
    img_file.seek(0, 2)
    if img_file.tell() > 20 * 1024 * 1024:
        return jsonify({"erro": "Imagem muito grande. Máximo 20MB."}), 400
    img_file.seek(0)
    edit_id = uuid.uuid4().hex[:12]
    save_path = os.path.join(THUMB_FOLDER, f"{current_user.id}_{edit_id}.png")
    img_file.save(save_path)
    # Salvar no banco de imagens
    salvar_no_banco("Thumbnail editada no Editor Visual", "", save_path, tipo="imagem", categoria="thumbnail")
    # Salvar no histórico de thumbnails
    _init_thumbnails_table()
    conn = sqlite3.connect('instance/veo3.db')
    conn.execute("INSERT INTO thumbnails (user_id, thumb_id, roteiro, prompt, estilo, path) VALUES (?,?,?,?,?,?)",
                 (current_user.id, edit_id, "Editor Visual", "salvo na biblioteca", "editor", save_path))
    conn.commit(); conn.close()
    return jsonify({"ok": True})

@app.route("/thumb_editor/historico")
@login_required
def thumb_editor_historico():
    _init_thumb_edits_table()
    conn = sqlite3.connect('instance/veo3.db')
    rows = conn.execute("SELECT edit_id, instrucao, criado_em FROM thumb_edits WHERE user_id=? ORDER BY id DESC LIMIT 30", (current_user.id,)).fetchall()
    conn.close()
    edits = [{"edit_id": r[0], "instrucao": r[1] or "", "criado_em": r[2] or ""} for r in rows if os.path.exists(os.path.join(THUMB_FOLDER, f"{current_user.id}_{r[0]}.png"))]
    return jsonify({"edits": edits})

# ── Suporte ──────────────────────────────────────────────
@app.route("/suporte", methods=["POST"])
@login_required
def contatar_suporte():
    assunto = request.json.get("assunto", "").strip()
    mensagem = request.json.get("mensagem", "").strip()
    if not assunto or not mensagem: return jsonify({"erro": "Preencha assunto e mensagem"}), 400
    # Salvar no banco
    try:
        conn = sqlite3.connect('instance/veo3.db')
        conn.execute("""CREATE TABLE IF NOT EXISTS suporte_msgs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, nome TEXT, email TEXT,
            plano TEXT, assunto TEXT, mensagem TEXT, lida INTEGER DEFAULT 0, criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        conn.execute("INSERT INTO suporte_msgs (user_id, nome, email, plano, assunto, mensagem) VALUES (?,?,?,?,?,?)",
                     (current_user.id, current_user.nome, current_user.email, current_user.plano or 'Sem plano', assunto, mensagem))
        conn.commit(); conn.close()
    except: pass
    # Tentar enviar email
    corpo = f"""<div style="font-family:Arial;max-width:600px;margin:0 auto;padding:20px">
        <h2 style="color:#4a9eff">Suporte — Klyonclaw Studio</h2>
        <p><b>De:</b> {current_user.nome} ({current_user.email})</p>
        <p><b>Plano:</b> {current_user.plano or 'Sem plano'} | <b>Créditos:</b> {current_user.creditos}</p>
        <p><b>Assunto:</b> {assunto}</p><hr/>
        <p style="white-space:pre-wrap">{mensagem}</p></div>"""
    enviar_email(os.environ.get("SUPPORT_EMAIL", "support@klyonclaw.com"), f"[Suporte] {assunto} — {current_user.nome}", corpo)
    return jsonify({"ok": True, "msg": "Mensagem enviada! Responderemos em breve."})

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))

@app.route("/admin/suporte_msgs")
@login_required
def admin_suporte_msgs():
    if not current_user.is_admin: return jsonify({"erro": "Sem permissao"}), 403
    try:
        conn = sqlite3.connect('instance/veo3.db')
        conn.execute("""CREATE TABLE IF NOT EXISTS suporte_msgs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, nome TEXT, email TEXT,
            plano TEXT, assunto TEXT, mensagem TEXT, lida INTEGER DEFAULT 0, criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        try: conn.execute("ALTER TABLE suporte_msgs ADD COLUMN lida INTEGER DEFAULT 0")
        except: pass
        rows = conn.execute("SELECT id, nome, email, plano, assunto, mensagem, criado_em, lida FROM suporte_msgs ORDER BY id DESC LIMIT 50").fetchall()
        # Marcar como lidas
        conn.execute("UPDATE suporte_msgs SET lida=1 WHERE lida=0")
        conn.commit(); conn.close()
        msgs = [{"id": r[0], "nome": r[1], "email": r[2], "plano": r[3], "assunto": r[4], "mensagem": r[5], "criado_em": r[6], "lida": r[7] if len(r) > 7 else 1} for r in rows]
        return jsonify({"msgs": msgs})
    except:
        return jsonify({"msgs": []})

@app.route("/admin/suporte_nao_lidas")
@login_required
def admin_suporte_nao_lidas():
    if not current_user.is_admin: return jsonify({"count": 0})
    try:
        conn = sqlite3.connect('instance/veo3.db')
        try: conn.execute("ALTER TABLE suporte_msgs ADD COLUMN lida INTEGER DEFAULT 0")
        except: pass
        count = conn.execute("SELECT COUNT(*) FROM suporte_msgs WHERE lida=0").fetchone()[0]
        conn.close()
        return jsonify({"count": count})
    except:
        return jsonify({"count": 0})

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
    cupom = data.get("cupom", "").strip()

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

    # Assinaturas e add-ons são recorrentes
    if plano_info["tipo"] in ("assinatura", "addon"):
        checkout_params["mode"] = "subscription"
        checkout_params["subscription_data"] = {
            "metadata": {
                "user_id": str(current_user.id),
                "plano_key": plano_info.get("key", ""),
                "creditos": str(plano_info.get("creditos", 0)),
                "tipo": plano_info.get("tipo", ""),
            }
        }
    else:
        checkout_params["mode"] = "payment"

    # Cupom de desconto
    if cupom:
        try:
            # Tentar como promotion code (código público tipo "DESCONTO10")
            promos = stripe.PromotionCode.list(code=cupom, active=True, limit=1)
            if promos.data:
                checkout_params["discounts"] = [{"promotion_code": promos.data[0].id}]
            else:
                # Tentar como coupon ID direto
                try:
                    stripe.Coupon.retrieve(cupom)
                    checkout_params["discounts"] = [{"coupon": cupom}]
                except:
                    return jsonify({"erro": "Cupom inválido ou expirado"}), 400
        except:
            return jsonify({"erro": "Cupom inválido ou expirado"}), 400
        # Remover allow_promotion_codes se tiver discounts
        checkout_params.pop("allow_promotion_codes", None)

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
                    elif plano_key == "audio":
                        current_user.audio_ativo = True

                db.session.commit()
        except Exception as e:
            print(f"Erro ao processar pagamento: {e}")
    return redirect(url_for("dashboard") + "?pagamento=sucesso")

@app.route("/stripe_webhook", methods=["POST"])
def stripe_webhook():
    if not STRIPE_WEBHOOK_SECRET:
        return jsonify({"erro": "Webhook não configurado"}), 500

    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError):
        return jsonify({"erro": "Webhook invalido"}), 400

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
                    elif plano_key == "audio":
                        user.audio_ativo = True
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
                           banco_addon_valor=BANCO_ADDON_VALOR,
                           audio_addon_price=AUDIO_ADDON_PRICE_ID,
                           audio_addon_valor=AUDIO_ADDON_VALOR)

@app.route("/perfil", methods=["GET", "POST"])
@login_required
def perfil():
    if request.method == "POST":
        data = request.json
        provider = data.get("provider", "").strip()
        api_key = data.get("api_key", "").strip()
        # Validar provider
        if provider and provider not in ("openai", "replicate"):
            return jsonify({"erro": "Provedor inválido"}), 400
        # Validar tamanhos
        image_size = data.get("image_size", "1024x1024")
        if image_size not in ("1024x1024", "1792x1024", "1024x1792"):
            image_size = "1024x1024"
        quality = data.get("quality", "standard")
        if quality not in ("standard", "hd"):
            quality = "standard"
        current_user.provider = provider
        current_user.api_key = api_key
        current_user.image_size = image_size
        current_user.quality = quality
        current_user.minimax_key = data.get("minimax_key", "").strip()
        current_user.minimax_group_id = data.get("minimax_group_id", "").strip()
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
    try:
        total_musicas = conn.execute("SELECT COUNT(*) FROM musicas_sistema WHERE tipo='musica' OR tipo IS NULL").fetchone()[0]
    except:
        total_musicas = 0
    try:
        total_efeitos = conn.execute("SELECT COUNT(*) FROM musicas_sistema WHERE tipo='efeito'").fetchone()[0]
    except:
        total_efeitos = 0
    conn.close()
    total_criacoes = Criacao.query.count()
    total_creditos = sum(u.creditos for u in users)
    users_com_plano = sum(1 for u in users if u.plano)
    from datetime import timedelta
    hoje = datetime.utcnow()
    users_recentes = sum(1 for u in users if (hoje - u.criado_em).days <= 7)
    # Visitas na landing
    try:
        conn2 = sqlite3.connect('instance/veo3.db')
        conn2.execute("CREATE TABLE IF NOT EXISTS visitas (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT, ip TEXT, criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        from datetime import date
        hoje_str = date.today().isoformat()
        total_visitas = conn2.execute("SELECT COUNT(*) FROM visitas").fetchone()[0]
        visitas_hoje = conn2.execute("SELECT COUNT(*) FROM visitas WHERE data=?", (hoje_str,)).fetchone()[0]
        visitas_7d = 0
        for i in range(7):
            dia = (date.today() - timedelta(days=i)).isoformat()
            visitas_7d += conn2.execute("SELECT COUNT(*) FROM visitas WHERE data=?", (dia,)).fetchone()[0]
        visitas_unicas = conn2.execute("SELECT COUNT(DISTINCT ip) FROM visitas").fetchone()[0]
        # Data da primeira visita registrada
        primeira_visita = conn2.execute("SELECT MIN(criado_em) FROM visitas").fetchone()[0]
        conn2.close()
        # Cadastros feitos DEPOIS do contador começar
        if primeira_visita:
            cadastros_pos_contador = sum(1 for u in users if u.criado_em and str(u.criado_em) >= str(primeira_visita))
        else:
            cadastros_pos_contador = 0
    except:
        total_visitas = 0
        visitas_hoje = 0
        visitas_7d = 0
        visitas_unicas = 0
        cadastros_pos_contador = 0
    return render_template("admin.html", users=users, prompts=prompts, total_imgs=total_imgs,
                           total_criacoes=total_criacoes, total_videos=total_videos,
                           total_musicas=total_musicas, total_efeitos=total_efeitos,
                           total_creditos=total_creditos, users_com_plano=users_com_plano,
                           users_recentes=users_recentes, planos=PLANOS_STRIPE,
                           total_visitas=total_visitas, visitas_hoje=visitas_hoje, visitas_7d=visitas_7d, visitas_unicas=visitas_unicas, cadastros_pos_contador=cadastros_pos_contador,
                           is_master=current_user.email == ADMIN_MASTER_EMAIL)

@app.route("/admin/toggle_admin", methods=["POST"])
@login_required
def admin_toggle():
    if not current_user.is_admin:
        return jsonify({"erro": "Sem permissao"}), 403
    # Só o admin master pode conceder/remover admin
    if current_user.email != ADMIN_MASTER_EMAIL:
        return jsonify({"erro": "Apenas o admin master pode alterar permissões de admin"}), 403
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

@app.route("/admin/toggle_audio", methods=["POST"])
@login_required
def admin_toggle_audio():
    if not current_user.is_admin:
        return jsonify({"erro": "Sem permissao"}), 403
    user_id = request.json.get("user_id")
    user = User.query.get(user_id)
    if user:
        user.audio_ativo = not user.audio_ativo
        db.session.commit()
        return jsonify({"ok": True, "audio_ativo": user.audio_ativo})
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
        if not SYSTEM_OPENAI_KEY:
            resultado["openai"] = {"status": "erro", "msg": "Chave não configurada (OPENAI_API_KEY)"}
        else:
            headers = {"Authorization": f"Bearer {SYSTEM_OPENAI_KEY}"}
            r = requests.get("https://api.openai.com/v1/models", headers=headers, timeout=10)
            if r.ok:
                resultado["openai"] = {"status": "ok", "msg": "API funcionando"}
            else:
                resultado["openai"] = {"status": "erro", "msg": "Chave inválida — verifique OPENAI_API_KEY"}
    except Exception as e:
        resultado["openai"] = {"status": "erro", "msg": "Erro de conexão"}
    # Verificar MiniMax
    try:
        if not SYSTEM_MINIMAX_KEY:
            resultado["minimax"] = {"status": "erro", "msg": "Chave não configurada (MINIMAX_API_KEY)"}
        else:
            headers = {"Authorization": f"Bearer {SYSTEM_MINIMAX_KEY}"}
            r = requests.get("https://api.minimax.io/v1/query/video_generation", headers=headers, params={"task_id": "test"}, timeout=10)
            if r.status_code != 401:
                resultado["minimax"] = {"status": "ok", "msg": "API funcionando"}
            else:
                resultado["minimax"] = {"status": "erro", "msg": "Chave inválida"}
    except Exception as e:
        resultado["minimax"] = {"status": "erro", "msg": "Erro de conexão"}
    # Verificar Stripe
    try:
        if not STRIPE_SECRET_KEY:
            resultado["stripe"] = {"status": "erro", "msg": "Chave não configurada (STRIPE_SECRET_KEY)"}
        else:
            bal = stripe.Balance.retrieve()
            saldo_brl = sum(b.amount/100 for b in bal.available if b.currency == "brl")
            resultado["stripe"] = {"status": "ok", "msg": f"Saldo: R${saldo_brl:.2f}"}
    except Exception as e:
        resultado["stripe"] = {"status": "erro", "msg": "Chave inválida — verifique STRIPE_SECRET_KEY"}
    return jsonify(resultado)

BRANDING_FILE = "branding_config.json"

# ── Atualizar nomes dos produtos na Stripe ──
def atualizar_branding_stripe():
    """Atualiza os nomes dos produtos na Stripe para incluir Klyonclaw Studio"""
    if not STRIPE_SECRET_KEY:
        return
    try:
        import sys
        # Coletar todos os price_ids usados
        all_prices = set()
        for p in PLANOS_STRIPE.values():
            if p.get("price_id"):
                all_prices.add(p["price_id"])
        for p in PACOTES_AVULSO.values():
            if p.get("price_id"):
                all_prices.add(p["price_id"])
        if BANCO_ADDON_PRICE_ID:
            all_prices.add(BANCO_ADDON_PRICE_ID)
        if AUDIO_ADDON_PRICE_ID:
            all_prices.add(AUDIO_ADDON_PRICE_ID)

        # Para cada price, pegar o produto e atualizar o nome
        produtos_atualizados = set()
        for pid in all_prices:
            try:
                price = stripe.Price.retrieve(pid)
                product_id = price.product
                if product_id in produtos_atualizados:
                    continue
                product = stripe.Product.retrieve(product_id)
                nome_atual = product.name or ""
                # Só atualizar se não tem "Klyonclaw" no nome
                if "Klyonclaw" not in nome_atual and "klyonclaw" not in nome_atual.lower():
                    # Buscar nome do plano no nosso mapa
                    plano_info = PRICE_MAP.get(pid, {})
                    novo_nome = f"Klyonclaw Studio — {plano_info.get('nome', nome_atual)}"
                    stripe.Product.modify(product_id, name=novo_nome)
                    sys.stderr.write(f"[STRIPE] Produto {product_id}: '{nome_atual}' → '{novo_nome}'\n")
                    sys.stderr.flush()
                produtos_atualizados.add(product_id)
            except Exception as e:
                sys.stderr.write(f"[STRIPE] Erro ao atualizar {pid}: {e}\n")
                sys.stderr.flush()
        if produtos_atualizados:
            sys.stderr.write(f"[STRIPE] {len(produtos_atualizados)} produtos atualizados com branding Klyonclaw Studio\n")
            sys.stderr.flush()
    except Exception as e:
        import sys
        sys.stderr.write(f"[STRIPE] Erro geral: {e}\n"); sys.stderr.flush()

def load_branding():
    try:
        if os.path.exists(BRANDING_FILE):
            with open(BRANDING_FILE) as f:
                return json.load(f)
    except: pass
    return {"cor_primaria": "#1a2332", "cor_accent": "#4a9eff", "nome": "Klyonclaw Studio", "subtitulo": "AI Video Automation", "logo": "", "icone": ""}

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
            except: pass

        if "icone" in request.files and request.files["icone"].filename:
            icone = request.files["icone"]
            icone_path = os.path.join("static", "icone.png")
            icone.save(icone_path)
            branding["icone"] = "/static/icone.png"

        save_branding(branding)
        return jsonify({"ok": True})
    return jsonify(load_branding())

# ── Demo Videos (Landing Page) ──
DEMOS_FILE = "demos_config.json"

def load_demos():
    try:
        if os.path.exists(DEMOS_FILE):
            with open(DEMOS_FILE) as f:
                return json.load(f)
    except: pass
    return []

def save_demos(demos):
    with open(DEMOS_FILE, "w") as f:
        json.dump(demos, f, indent=2)

@app.route("/admin/demos", methods=["GET"])
@login_required
def admin_demos_get():
    if not current_user.is_admin:
        return jsonify({"erro": "Sem permissao"}), 403
    return jsonify({"demos": load_demos()})

@app.route("/admin/demos/upload", methods=["POST"])
@login_required
def admin_demos_upload():
    if not current_user.is_admin:
        return jsonify({"erro": "Sem permissao"}), 403
    if "video" not in request.files or not request.files["video"].filename:
        return jsonify({"erro": "Envie um vídeo"}), 400
    video = request.files["video"]
    titulo = request.form.get("titulo", "").strip() or "Vídeo demo"
    descricao = request.form.get("descricao", "").strip() or "Gerado com Klyonclaw Studio"
    os.makedirs("static", exist_ok=True)
    demos = load_demos()
    idx = len(demos) + 1
    filename = f"demo_{uuid.uuid4().hex[:8]}.mp4"
    filepath = os.path.join("static", filename)
    video.save(filepath)
    demos.append({"filename": filename, "titulo": titulo, "descricao": descricao, "path": f"/static/{filename}"})
    save_demos(demos)
    return jsonify({"ok": True})

@app.route("/admin/demos/delete", methods=["POST"])
@login_required
def admin_demos_delete():
    if not current_user.is_admin:
        return jsonify({"erro": "Sem permissao"}), 403
    data = request.json
    idx = data.get("index", -1)
    demos = load_demos()
    if 0 <= idx < len(demos):
        # Deletar arquivo
        filepath = os.path.join("static", demos[idx]["filename"])
        if os.path.exists(filepath):
            os.remove(filepath)
        demos.pop(idx)
        save_demos(demos)
    return jsonify({"ok": True})

@app.route("/api/demos")
def api_demos():
    """Retorna demos para a landing page (público)"""
    return jsonify({"demos": load_demos()})

@app.route("/static/<path:filename>")
def static_files(filename):
    if ".." in filename or filename.startswith("/"):
        return jsonify({"erro": "Acesso negado"}), 403
    filepath = os.path.normpath(os.path.join("static", filename))
    if not filepath.startswith(os.path.normpath("static")):
        return jsonify({"erro": "Acesso negado"}), 403
    if not os.path.exists(filepath):
        return jsonify({"erro": "Não encontrado"}), 404
    response = send_file(filepath)
    response.headers['Cache-Control'] = 'public, max-age=604800'
    return response

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

@app.route("/admin/upload_banco", methods=["POST"])
@login_required
def admin_upload_banco():
    if not current_user.is_admin:
        return jsonify({"erro": "Sem permissao"}), 403
    if "arquivo" not in request.files or not request.files["arquivo"].filename:
        return jsonify({"erro": "Envie um arquivo"}), 400
    arquivo = request.files["arquivo"]
    filename = arquivo.filename.lower()
    if not filename.endswith((".png", ".jpg", ".jpeg", ".webp", ".mp4")):
        return jsonify({"erro": "Formato inválido. Use PNG, JPG, WEBP ou MP4"}), 400
    arquivo.seek(0, 2)
    if arquivo.tell() > 100 * 1024 * 1024:
        return jsonify({"erro": "Arquivo muito grande. Máximo 100MB."}), 400
    arquivo.seek(0)
    ext = filename.rsplit(".", 1)[-1]
    nome = f"{uuid.uuid4().hex[:12]}.{ext}"
    destino = os.path.join(BANCO_IMG_FOLDER, nome)
    arquivo.save(destino)
    tipo = "video" if ext == "mp4" else "imagem"
    # Auto-categorizar com GPT (mesma lógica das músicas)
    categoria = "admin_upload"
    descricao = "Upload manual pelo admin"
    tags = "upload admin"
    try:
        api_key = current_user.get_api_key()
        if api_key:
            import base64 as b64mod
            headers_ai = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            categorias_banco = ["paisagem","pessoa","animal","tecnologia","fantasia","urbano","natureza","comida","esporte","negocio","educacao","saude","musica_arte","transporte","abstrato","geral"]
            if tipo == "video":
                # Extrair frame do vídeo pra analisar
                frame_path = os.path.join(UPLOAD_FOLDER, f"frame_cat_{uuid.uuid4().hex[:6]}.png")
                try:
                    subprocess.run(["ffmpeg", "-y", "-i", destino, "-vframes", "1", "-q:v", "2", frame_path],
                                   capture_output=True, text=True, timeout=10)
                    if os.path.exists(frame_path):
                        with open(frame_path, "rb") as fimg:
                            b64data = b64mod.b64encode(fimg.read()).decode()
                        body = {"model": "gpt-4o-mini", "messages": [
                            {"role": "system", "content": f"""Analise este frame de vídeo e retorne:
1. CATEGORIAS: todas que se encaixam, separadas por vírgula. Opções: {', '.join(categorias_banco)}
2. DESCRICAO: descrição curta em português (max 80 chars)
3. TAGS: 5-8 palavras-chave de busca em português, separadas por vírgula

Formato:
CATEGORIAS: ...
DESCRICAO: ...
TAGS: ..."""},
                            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64data}"}}]}
                        ], "max_tokens": 200}
                        r = requests.post("https://api.openai.com/v1/chat/completions", headers=headers_ai, json=body, timeout=20)
                        if r.ok:
                            texto = r.json()["choices"][0]["message"]["content"].strip()
                            for linha in texto.split("\n"):
                                if linha.upper().startswith("CATEGORIAS:") or linha.upper().startswith("CATEGORIA:"):
                                    cats = [c.strip() for c in linha.split(":", 1)[1].split(",") if c.strip() in categorias_banco]
                                    if cats: categoria = ",".join(cats)
                                elif linha.upper().startswith("DESCRICAO:") or linha.upper().startswith("DESCRIÇÃO:"):
                                    descricao = linha.split(":", 1)[1].strip() or descricao
                                elif linha.upper().startswith("TAGS:"):
                                    tags = linha.split(":", 1)[1].strip().lower() or tags
                        os.remove(frame_path)
                except: pass
            else:
                # Analisar imagem diretamente
                try:
                    with open(destino, "rb") as fimg:
                        b64data = b64mod.b64encode(fimg.read()).decode()
                    body = {"model": "gpt-4o-mini", "messages": [
                        {"role": "system", "content": f"""Analise esta imagem e retorne:
1. CATEGORIAS: todas que se encaixam, separadas por vírgula. Opções: {', '.join(categorias_banco)}
2. DESCRICAO: descrição curta em português (max 80 chars)
3. TAGS: 5-8 palavras-chave de busca em português, separadas por vírgula

Formato:
CATEGORIAS: ...
DESCRICAO: ...
TAGS: ..."""},
                        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64data}"}}]}
                    ], "max_tokens": 200}
                    r = requests.post("https://api.openai.com/v1/chat/completions", headers=headers_ai, json=body, timeout=20)
                    if r.ok:
                        texto = r.json()["choices"][0]["message"]["content"].strip()
                        for linha in texto.split("\n"):
                            if linha.upper().startswith("CATEGORIAS:") or linha.upper().startswith("CATEGORIA:"):
                                cats = [c.strip() for c in linha.split(":", 1)[1].split(",") if c.strip() in categorias_banco]
                                if cats: categoria = ",".join(cats)
                            elif linha.upper().startswith("DESCRICAO:") or linha.upper().startswith("DESCRIÇÃO:"):
                                descricao = linha.split(":", 1)[1].strip() or descricao
                            elif linha.upper().startswith("TAGS:"):
                                tags = linha.split(":", 1)[1].strip().lower() or tags
                except: pass
    except: pass
    conn = sqlite3.connect('instance/veo3.db')
    conn.execute("INSERT INTO banco_imagens (prompt, estilo, tags, path, tipo, categoria, descricao) VALUES (?,?,?,?,?,?,?)",
                 (descricao, "", tags, destino, tipo, categoria, descricao))
    conn.commit(); conn.close()
    return jsonify({"ok": True, "categoria": categoria, "descricao": descricao})

@app.route("/admin/renomear_banco_ia", methods=["POST"])
@login_required
def admin_renomear_banco_ia():
    if not current_user.is_admin:
        return jsonify({"erro": "Sem permissao"}), 403
    api_key = current_user.get_api_key()
    if not api_key:
        return jsonify({"erro": "Sem API key"}), 400
    import base64, sys
    try:
        conn = sqlite3.connect('instance/veo3.db')
        rows = conn.execute("SELECT id, path, tipo FROM banco_imagens ORDER BY id DESC LIMIT 200").fetchall()
        total = 0
        categorias_banco = ["paisagem","pessoa","animal","tecnologia","fantasia","urbano","natureza","comida","esporte","negocio","educacao","saude","musica_arte","transporte","abstrato","geral"]
        cat_list_str = ", ".join(categorias_banco)
        sys_prompt_video = f"""Este é um frame de um vídeo animado. Retorne:
1. CATEGORIAS: todas que se encaixam, separadas por vírgula. Opções: {cat_list_str}
2. DESCRICAO: descrição curta em português (max 80 chars)
3. TAGS: 5-8 palavras-chave de busca em português, separadas por vírgula

Formato:
CATEGORIAS: ...
DESCRICAO: ...
TAGS: ..."""
        sys_prompt_img = f"""Analise esta imagem e retorne:
1. CATEGORIAS: todas que se encaixam, separadas por vírgula. Opções: {cat_list_str}
2. DESCRICAO: descrição curta em português (max 80 chars)
3. TAGS: 5-8 palavras-chave de busca em português, separadas por vírgula

Formato:
CATEGORIAS: ...
DESCRICAO: ...
TAGS: ..."""
        for row in rows:
            img_id, img_path, tipo = row[0], row[1], row[2] if len(row) > 2 else "imagem"
            if not os.path.exists(img_path):
                continue
            if tipo == "video":
                # Extrair primeiro frame do vídeo pra analisar
                try:
                    frame_path = os.path.join(UPLOAD_FOLDER, f"frame_temp_{img_id}.png")
                    subprocess.run(["ffmpeg", "-y", "-i", img_path, "-vframes", "1", "-q:v", "2", frame_path],
                                   capture_output=True, text=True, timeout=10)
                    if os.path.exists(frame_path):
                        with open(frame_path, "rb") as f:
                            b64 = base64.b64encode(f.read()).decode()
                        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
                        body = {"model": "gpt-4o-mini", "messages": [
                            {"role": "system", "content": sys_prompt_video},
                            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]}
                        ], "max_tokens": 200}
                        r = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=body, timeout=20)
                        if r.ok:
                            texto = r.json()["choices"][0]["message"]["content"].strip()
                            descricao = ""
                            tags = ""
                            categoria = ""
                            for linha in texto.split("\n"):
                                if linha.upper().startswith("CATEGORIAS:") or linha.upper().startswith("CATEGORIA:"):
                                    cats = [c.strip() for c in linha.split(":", 1)[1].split(",") if c.strip() in categorias_banco]
                                    if cats: categoria = ",".join(cats)
                                elif linha.upper().startswith("DESCRICAO:") or linha.upper().startswith("DESCRIÇÃO:"):
                                    descricao = linha.split(":", 1)[1].strip()
                                elif linha.upper().startswith("TAGS:"):
                                    tags = linha.split(":", 1)[1].strip().lower()
                            if descricao or tags or categoria:
                                conn.execute("UPDATE banco_imagens SET descricao=?, tags=?, categoria=? WHERE id=?",
                                             (descricao or "Vídeo animado", tags or "video,animacao", categoria or "geral", img_id))
                                total += 1
                                sys.stderr.write(f"[RENOMEAR] Video #{img_id}: {descricao} [{categoria}]\n"); sys.stderr.flush()
                        os.remove(frame_path)
                    else:
                        conn.execute("UPDATE banco_imagens SET descricao=?, tags=?, categoria=? WHERE id=?",
                                     ("Vídeo animado", "video,animacao,cena", "geral", img_id))
                        total += 1
                except:
                    conn.execute("UPDATE banco_imagens SET descricao=?, tags=?, categoria=? WHERE id=?",
                                 ("Vídeo animado", "video,animacao,cena", "geral", img_id))
                    total += 1
                continue
            # Analisar imagem com GPT-4o-mini visão
            try:
                with open(img_path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode()
                headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
                body = {"model": "gpt-4o-mini", "messages": [
                    {"role": "system", "content": sys_prompt_img},
                    {"role": "user", "content": [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]}
                ], "max_tokens": 200}
                r = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=body, timeout=20)
                if r.ok:
                    texto = r.json()["choices"][0]["message"]["content"].strip()
                    descricao = ""
                    tags = ""
                    categoria = ""
                    for linha in texto.split("\n"):
                        if linha.upper().startswith("CATEGORIAS:") or linha.upper().startswith("CATEGORIA:"):
                            cats = [c.strip() for c in linha.split(":", 1)[1].split(",") if c.strip() in categorias_banco]
                            if cats: categoria = ",".join(cats)
                        elif linha.upper().startswith("DESCRICAO:") or linha.upper().startswith("DESCRIÇÃO:"):
                            descricao = linha.split(":", 1)[1].strip()
                        elif linha.upper().startswith("TAGS:"):
                            tags = linha.split(":", 1)[1].strip().lower()
                    if descricao or tags or categoria:
                        conn.execute("UPDATE banco_imagens SET descricao=?, tags=?, categoria=? WHERE id=?",
                                     (descricao or "Imagem", tags or "", categoria or "geral", img_id))
                        total += 1
                        sys.stderr.write(f"[RENOMEAR] #{img_id}: {descricao} [{categoria}]\n"); sys.stderr.flush()
            except Exception as e:
                sys.stderr.write(f"[RENOMEAR] #{img_id} erro: {e}\n"); sys.stderr.flush()
                continue
        conn.commit(); conn.close()
        return jsonify({"ok": True, "total": total})
    except Exception as e:
        return jsonify({"erro": str(e)}), 500

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

@app.route("/admin/user_acoes/<int:user_id>")
@login_required
def admin_user_acoes(user_id):
    if not current_user.is_admin:
        return jsonify({"erro": "Sem permissao"}), 403
    try:
        conn = sqlite3.connect('instance/veo3.db')
        conn.execute("CREATE TABLE IF NOT EXISTS user_acoes (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, acao TEXT, detalhe TEXT, criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        rows = conn.execute("SELECT acao, detalhe, criado_em FROM user_acoes WHERE user_id=? ORDER BY id DESC LIMIT 20", (user_id,)).fetchall()
        conn.close()
        acoes = [{"acao": r[0], "detalhe": r[1], "data": r[2]} for r in rows]
        return jsonify({"acoes": acoes})
    except:
        return jsonify({"acoes": []})

# ── Convites de Teste ──
@app.route("/admin/enviar_convite_teste", methods=["POST"])
@login_required
def admin_enviar_convite_teste():
    if not current_user.is_admin:
        return jsonify({"erro": "Sem permissao"}), 403
    try:
        conn = sqlite3.connect('instance/veo3.db')
        conn.execute("CREATE TABLE IF NOT EXISTS convites_teste (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, email TEXT, codigo TEXT, usado INTEGER DEFAULT 0, criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        ja_convidados = [r[0] for r in conn.execute("SELECT user_id FROM convites_teste").fetchall()]
        conn.close()
    except:
        ja_convidados = []
    usuarios = [u for u in User.query.order_by(User.criado_em.desc()).all()
                if not u.plano and not u.is_admin and u.id not in ja_convidados]
    if not usuarios:
        return jsonify({"erro": "Nenhum usuário elegível"}), 400
    selecionados = usuarios[:2]
    enviados = []
    for user in selecionados:
        codigo = f"TESTE-{uuid.uuid4().hex[:6].upper()}"
        try:
            conn = sqlite3.connect('instance/veo3.db')
            conn.execute("INSERT INTO convites_teste (user_id, email, codigo) VALUES (?, ?, ?)", (user.id, user.email, codigo))
            conn.commit(); conn.close()
        except: continue
        enviar_email(user.email, "🎬 Você foi selecionado para testar o Klyonclaw Studio!", f"""
        <div style="font-family:Arial;max-width:500px;margin:0 auto;padding:20px;background:#0b1120;color:#e2e8f0;border-radius:12px">
            <h1 style="color:#4a9eff">Klyonclaw Studio</h1>
            <p>Olá <b>{user.nome}</b>! 👋</p>
            <p style="font-size:16px;line-height:1.6">Você foi <b style="color:#4a9eff">selecionado(a)</b> para testar gratuitamente nossa plataforma de criação de vídeos com IA!</p>
            <p>Para ativar seu acesso:</p>
            <ol style="font-size:15px;line-height:2;padding-left:20px">
                <li>Acesse <a href="https://studio.klyonclaw.com/dashboard" style="color:#4a9eff">studio.klyonclaw.com</a></li>
                <li>Na aba <b>Criar</b>, clique em <b>🎁 Tenho um código</b></li>
                <li>Digite o código abaixo</li>
            </ol>
            <div style="background:#1e3a5f;border:2px solid #4a9eff;border-radius:10px;padding:20px;text-align:center;margin:20px 0">
                <div style="font-size:12px;color:#94a3b8;margin-bottom:8px">Seu código de teste</div>
                <div style="font-size:28px;font-weight:800;color:#4a9eff;letter-spacing:4px">{codigo}</div>
            </div>
            <p style="color:#ef4444;font-size:14px;font-weight:600">⏰ Este código expira em 2 horas.</p>
            <a href="https://studio.klyonclaw.com/dashboard" style="display:inline-block;padding:14px 28px;background:#2563eb;color:#fff;border-radius:10px;text-decoration:none;font-weight:700">Acessar agora →</a>
            <p style="color:#475569;font-size:11px;margin-top:24px">Klyonclaw Studio — AI Video Automation</p>
        </div>""")
        enviados.append({"nome": user.nome, "email": user.email, "codigo": codigo})
    return jsonify({"ok": True, "enviados": enviados})

@app.route("/admin/convites_teste")
@login_required
def admin_listar_convites():
    if not current_user.is_admin:
        return jsonify({"erro": "Sem permissao"}), 403
    try:
        conn = sqlite3.connect('instance/veo3.db')
        conn.execute("CREATE TABLE IF NOT EXISTS convites_teste (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, email TEXT, codigo TEXT, usado INTEGER DEFAULT 0, criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        rows = conn.execute("SELECT email, codigo, usado, criado_em FROM convites_teste ORDER BY id DESC LIMIT 20").fetchall()
        conn.close()
        return jsonify({"convites": [{"email": r[0], "codigo": r[1], "usado": bool(r[2]), "data": r[3]} for r in rows]})
    except:
        return jsonify({"convites": []})

@app.route("/validar_codigo_teste", methods=["POST"])
@login_required
def validar_codigo_teste():
    codigo = request.json.get("codigo", "").strip().upper()
    if not codigo:
        return jsonify({"erro": "Digite o código"}), 400
    try:
        conn = sqlite3.connect('instance/veo3.db')
        conn.execute("CREATE TABLE IF NOT EXISTS convites_teste (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, email TEXT, codigo TEXT, usado INTEGER DEFAULT 0, criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        # Já usou código antes?
        ja_usou = conn.execute("SELECT id FROM convites_teste WHERE user_id=? AND usado=1", (current_user.id,)).fetchone()
        if ja_usou:
            conn.close()
            return jsonify({"erro": "Você já utilizou um código de teste anteriormente."}), 400
        row = conn.execute("SELECT id, user_id, usado, criado_em FROM convites_teste WHERE codigo=?", (codigo,)).fetchone()
        if not row:
            conn.close()
            enviar_email(current_user.email, "❌ Código inválido — Klyonclaw Studio", f"""
            <div style="font-family:Arial;max-width:500px;margin:0 auto;padding:20px"><h1 style="color:#4a9eff">Klyonclaw Studio</h1>
            <p>Olá <b>{current_user.nome}</b>, o código <b style="color:#ef4444">{codigo}</b> não é válido.</p></div>""")
            return jsonify({"erro": "Código inválido."}), 400
        convite_id, convite_user_id, usado, criado_em = row
        if usado:
            conn.close()
            return jsonify({"erro": "Este código já foi utilizado."}), 400
        if convite_user_id != current_user.id:
            conn.close()
            return jsonify({"erro": "Este código não pertence à sua conta."}), 400
        from datetime import timedelta
        try:
            criado = datetime.strptime(criado_em, "%Y-%m-%d %H:%M:%S")
        except:
            criado = datetime.strptime(criado_em.split(".")[0], "%Y-%m-%d %H:%M:%S")
        if datetime.utcnow() - criado > timedelta(hours=2):
            conn.close()
            enviar_email(current_user.email, "⏰ Código expirado — Klyonclaw Studio", f"""
            <div style="font-family:Arial;max-width:500px;margin:0 auto;padding:20px"><h1 style="color:#4a9eff">Klyonclaw Studio</h1>
            <p>Olá <b>{current_user.nome}</b>, o código <b style="color:#ef4444">{codigo}</b> expirou (validade: 2 horas).</p>
            <p>Assine um plano para começar a criar:</p>
            <a href="https://studio.klyonclaw.com/dashboard" style="display:inline-block;padding:12px 24px;background:#2563eb;color:#fff;border-radius:8px;text-decoration:none;font-weight:600">Ver Planos →</a></div>""")
            return jsonify({"erro": "Código expirado (validade: 2 horas)."}), 400
        # Sucesso — ativar tester
        conn.execute("UPDATE convites_teste SET usado=1 WHERE id=?", (convite_id,))
        conn.commit(); conn.close()
        current_user.plano = "tester"
        current_user.creditos = 200
        db.session.commit()
        enviar_email(current_user.email, "✅ Teste ativado! — Klyonclaw Studio", f"""
        <div style="font-family:Arial;max-width:500px;margin:0 auto;padding:20px"><h1 style="color:#4a9eff">Klyonclaw Studio</h1>
        <p>Olá <b>{current_user.nome}</b>! 🎉 Seu teste foi ativado!</p>
        <p>Você recebeu <b style="color:#4a9eff">200 créditos</b> para criar seus primeiros vídeos.</p>
        <a href="https://studio.klyonclaw.com/dashboard" style="display:inline-block;padding:14px 28px;background:#2563eb;color:#fff;border-radius:10px;text-decoration:none;font-weight:700">Criar meu primeiro vídeo →</a></div>""")
        return jsonify({"ok": True})
    except Exception as e:
        import sys; sys.stderr.write(f"[CONVITE] Erro: {e}\n"); sys.stderr.flush()
        return jsonify({"erro": "Erro ao validar código"}), 500

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
    if ".." in filename or filename.startswith("/"):
        return jsonify({"erro": "Acesso negado"}), 403
    filepath = os.path.normpath(os.path.join(BANCO_IMG_FOLDER, filename))
    if not filepath.startswith(os.path.normpath(BANCO_IMG_FOLDER)):
        return jsonify({"erro": "Acesso negado"}), 403
    if not os.path.exists(filepath):
        return jsonify({"erro": "Arquivo não encontrado"}), 404
    response = send_file(filepath)
    response.headers['Cache-Control'] = 'public, max-age=86400'
    return response

@app.route("/buscar_banco", methods=["POST"])
@login_required
def buscar_banco():
    termo = request.json.get("termo", "").lower().strip()
    tipo_filtro = request.json.get("tipo", "")
    estilo = request.json.get("estilo", "")
    categoria_filtro = request.json.get("categoria", "")
    pagina = int(request.json.get("pagina", 1))
    por_pagina = 20
    offset = (pagina - 1) * por_pagina

    conn = sqlite3.connect('instance/veo3.db')
    query = "SELECT id, prompt, path, estilo, tipo, categoria, descricao FROM banco_imagens WHERE 1=1"
    count_query = "SELECT COUNT(*) FROM banco_imagens WHERE 1=1"
    params = []
    if termo:
        filtro = " AND (tags LIKE ? OR prompt LIKE ? OR descricao LIKE ? OR categoria LIKE ?)"
        query += filtro
        count_query += filtro
        params += [f"%{termo}%", f"%{termo}%", f"%{termo}%", f"%{termo}%"]
    if tipo_filtro:
        query += " AND tipo = ?"
        count_query += " AND tipo = ?"
        params.append(tipo_filtro)
    if estilo:
        query += " AND estilo = ?"
        count_query += " AND estilo = ?"
        params.append(estilo)
    if categoria_filtro:
        query += " AND categoria LIKE ?"
        count_query += " AND categoria LIKE ?"
        params.append(f"%{categoria_filtro}%")

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
    minimax_key = current_user.get_minimax_key()
    minimax_group = current_user.get_minimax_group_id()
    if not minimax_key:
        return jsonify({"erro": "Narração não disponível. Assine um plano para usar esta funcionalidade."}), 400
    if "audio" not in request.files or not request.files["audio"].filename:
        return jsonify({"erro": "Envie um arquivo de audio"}), 400
    nome_voz = request.form.get("nome_voz", "").strip()
    if not nome_voz:
        return jsonify({"erro": "Digite um nome para a voz"}), 400
    audio = request.files["audio"]
    if not audio.filename.lower().endswith((".mp3", ".wav", ".m4a", ".ogg")):
        return jsonify({"erro": "Formato inválido. Use MP3, WAV, M4A ou OGG"}), 400
    audio.seek(0, 2)
    if audio.tell() > 30 * 1024 * 1024:
        return jsonify({"erro": "Arquivo muito grande. Máximo 30MB."}), 400
    audio.seek(0)
    voice_id = f"user_{current_user.id}_{uuid.uuid4().hex[:8]}"
    caminho = os.path.join(UPLOAD_FOLDER, f"{voice_id}.mp3")
    audio.save(caminho)
    try:
        clonar_voz_minimax(minimax_key, minimax_group, caminho, voice_id)
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

# ── Preview de Voz ───────────────────────────────────────
VOICE_PREVIEW_FOLDER = "voice_previews"
os.makedirs(VOICE_PREVIEW_FOLDER, exist_ok=True)

@app.route("/preview_voz", methods=["POST"])
@login_required
def preview_voz():
    """Gera um áudio curto de preview da voz selecionada (cacheado)"""
    voice_id = request.json.get("voice_id", "").strip()
    if not voice_id:
        return jsonify({"erro": "Selecione uma voz"}), 400
    # Verificar cache
    cache_path = os.path.join(VOICE_PREVIEW_FOLDER, f"{voice_id}.mp3")
    if os.path.exists(cache_path):
        return send_file(cache_path, mimetype="audio/mpeg")
    # Gerar preview
    minimax_key = current_user.get_minimax_key()
    minimax_gid = current_user.get_minimax_group_id()
    if not minimax_key:
        minimax_key = SYSTEM_MINIMAX_KEY
        minimax_gid = SYSTEM_MINIMAX_GROUP_ID
    if not minimax_key:
        return jsonify({"erro": "API de voz não configurada"}), 400
    try:
        texto_preview = "Olá, esta é uma prévia da minha voz. Espero que goste do resultado."
        gerar_audio_minimax(texto_preview, minimax_key, minimax_gid, voice_id, cache_path)
        return send_file(cache_path, mimetype="audio/mpeg")
    except Exception as e:
        return jsonify({"erro": "Erro ao gerar preview"}), 500

# ── Rotas Música ─────────────────────────────────────────
@app.route("/upload_musica", methods=["POST"])
@login_required
def upload_musica():
    if "musica" not in request.files or not request.files["musica"].filename:
        return jsonify({"erro": "Envie um arquivo de música"}), 400
    musica = request.files["musica"]
    nome = request.form.get("nome", "").strip() or musica.filename
    if not musica.filename.lower().endswith((".mp3", ".wav", ".m4a", ".ogg")):
        return jsonify({"erro": "Formato inválido. Use MP3, WAV, M4A ou OGG"}), 400
    # Verificar tamanho (max 50MB)
    musica.seek(0, 2)
    size = musica.tell()
    musica.seek(0)
    if size > 50 * 1024 * 1024:
        return jsonify({"erro": "Arquivo muito grande. Máximo 50MB."}), 400
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
    if ".." in filename or filename.startswith("/"):
        return jsonify({"erro": "Acesso negado"}), 403
    filepath = os.path.normpath(os.path.join(MUSICAS_FOLDER, filename))
    if not filepath.startswith(os.path.normpath(MUSICAS_FOLDER)):
        return jsonify({"erro": "Acesso negado"}), 403
    try:
        conn = sqlite3.connect('instance/veo3.db')
        row = conn.execute("SELECT id FROM musicas WHERE user_id=? AND path=?", (current_user.id, filepath)).fetchone()
        conn.close()
        if not row:
            return jsonify({"erro": "Acesso negado"}), 403
    except:
        return jsonify({"erro": "Acesso negado"}), 403
    return send_file(filepath)

@app.route("/musicas_sistema")
@login_required
def musicas_sistema():
    """Lista músicas do sistema organizadas por categoria"""
    musicas = []
    try:
        conn = sqlite3.connect('instance/veo3.db')
        conn.execute("""CREATE TABLE IF NOT EXISTS musicas_sistema (
            id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT, categoria TEXT, path TEXT,
            tipo TEXT DEFAULT 'musica',
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        try: conn.execute("ALTER TABLE musicas_sistema ADD COLUMN tipo TEXT DEFAULT 'musica'")
        except: pass
        rows = conn.execute("SELECT id, nome, categoria, path, tipo FROM musicas_sistema WHERE tipo='musica' OR tipo IS NULL ORDER BY categoria, nome").fetchall()
        conn.close()
        for r in rows:
            if os.path.exists(os.path.join(MUSICAS_SISTEMA_FOLDER, r[3])):
                musicas.append({"id": f"sys_{r[0]}", "nome": r[1], "categoria": r[2], "path": r[3], "tipo_audio": r[4] or "musica"})
    except: pass
    return jsonify({"musicas": musicas})

@app.route("/sugerir_musica", methods=["POST"])
@login_required
def sugerir_musica():
    """GPT analisa o roteiro e sugere a melhor categoria de música do banco"""
    roteiro = request.json.get("roteiro", "").strip()
    if not roteiro:
        return jsonify({"erro": "Sem roteiro"}), 400
    api_key = current_user.get_api_key()
    if not api_key:
        return jsonify({"sugestao": "geral"})
    try:
        # Buscar categorias disponíveis no banco
        conn = sqlite3.connect('instance/veo3.db')
        try: conn.execute("ALTER TABLE musicas_sistema ADD COLUMN tipo TEXT DEFAULT 'musica'")
        except: pass
        rows = conn.execute("SELECT DISTINCT categoria FROM musicas_sistema WHERE tipo='musica' OR tipo IS NULL").fetchall()
        conn.close()
        categorias_disponiveis = []
        for r in rows:
            for c in (r[0] or "").split(","):
                c = c.strip()
                if c and c not in categorias_disponiveis:
                    categorias_disponiveis.append(c)
        if not categorias_disponiveis:
            return jsonify({"sugestao": "geral"})

        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        body = {"model": "gpt-4o-mini", "messages": [
            {"role": "system", "content": f"""Analise este roteiro de vídeo e escolha a MELHOR categoria de música de fundo.
Categorias disponíveis: {', '.join(categorias_disponiveis)}

Retorne APENAS o nome da categoria, nada mais. Exemplo: epico"""},
            {"role": "user", "content": roteiro[:500]}
        ], "max_tokens": 10}
        r = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=body, timeout=10)
        if r.ok:
            cat = r.json()["choices"][0]["message"]["content"].strip().lower()
            if cat in categorias_disponiveis:
                conn2 = sqlite3.connect('instance/veo3.db')
                mus = conn2.execute("SELECT id, nome, categoria FROM musicas_sistema WHERE (tipo='musica' OR tipo IS NULL) AND categoria LIKE ? ORDER BY RANDOM() LIMIT 1", (f"%{cat}%",)).fetchone()
                conn2.close()
                if mus:
                    return jsonify({"sugestao": cat, "musica_id": f"sys_{mus[0]}", "musica_nome": mus[1]})
            return jsonify({"sugestao": cat})
    except: pass
    return jsonify({"sugestao": "geral"})

@app.route("/musica_sistema/<path:filename>")
@login_required
def musica_sistema_file(filename):
    if ".." in filename or filename.startswith("/"):
        return jsonify({"erro": "Acesso negado"}), 403
    filepath = os.path.normpath(os.path.join(MUSICAS_SISTEMA_FOLDER, filename))
    if not filepath.startswith(os.path.normpath(MUSICAS_SISTEMA_FOLDER)):
        return jsonify({"erro": "Acesso negado"}), 403
    if not os.path.exists(filepath):
        return jsonify({"erro": "Não encontrado"}), 404
    return send_file(filepath)

@app.route("/jamendo/buscar")
@login_required
def jamendo_buscar():
    """Busca músicas na Jamendo API por tag/mood"""
    tag = request.args.get("tag", "").strip()
    busca = request.args.get("q", "").strip()
    pagina = int(request.args.get("pagina", 1))
    try:
        params = {
            "client_id": JAMENDO_CLIENT_ID,
            "format": "json",
            "limit": 20,
            "offset": (pagina - 1) * 20,
            "include": "musicinfo",
            "audioformat": "mp32",
        }
        if tag:
            params["tags"] = tag
            params["order"] = "popularity_total"
        elif busca:
            params["namesearch"] = busca
            params["order"] = "relevance"
        else:
            params["order"] = "popularity_total"

        r = requests.get("https://api.jamendo.com/v3.0/tracks", params=params, timeout=10)
        if r.ok:
            data = r.json()
            tracks = []
            for t in data.get("results", []):
                tracks.append({
                    "id": f"jam_{t['id']}",
                    "nome": t.get("name", ""),
                    "artista": t.get("artist_name", ""),
                    "duracao": t.get("duration", 0),
                    "audio_url": t.get("audio", ""),
                    "audiodownload": t.get("audiodownload", ""),
                    "tags": ", ".join(t.get("musicinfo", {}).get("tags", {}).get("genres", [])),
                    "categoria": "jamendo",
                })
            return jsonify({"ok": True, "tracks": tracks, "total": data.get("headers", {}).get("results_fullcount", 0)})
    except Exception as e:
        return jsonify({"erro": str(e)}), 500
    return jsonify({"tracks": [], "total": 0})

@app.route("/jamendo/download", methods=["POST"])
@login_required
def jamendo_download():
    """Baixa uma música da Jamendo e salva como música do sistema"""
    audio_url = request.json.get("audio_url", "")
    nome = request.json.get("nome", "")
    tag = request.json.get("tag", "geral")
    if not audio_url or not nome:
        return jsonify({"erro": "Dados incompletos"}), 400
    try:
        r = requests.get(audio_url, timeout=60)
        if r.ok:
            filename = f"jam_{uuid.uuid4().hex[:8]}.mp3"
            filepath = os.path.join(MUSICAS_SISTEMA_FOLDER, filename)
            with open(filepath, "wb") as f:
                f.write(r.content)
            # Salvar no banco
            conn = sqlite3.connect('instance/veo3.db')
            conn.execute("""CREATE TABLE IF NOT EXISTS musicas_sistema (
                id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT, categoria TEXT, path TEXT,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
            conn.execute("INSERT INTO musicas_sistema (nome, categoria, path) VALUES (?,?,?)", (nome, tag, filename))
            conn.commit(); conn.close()
            return jsonify({"ok": True})
        return jsonify({"erro": "Erro ao baixar"}), 500
    except Exception as e:
        return jsonify({"erro": str(e)}), 500

@app.route("/admin/upload_musica_sistema", methods=["POST"])
@login_required
def admin_upload_musica_sistema():
    if not current_user.is_admin:
        return jsonify({"erro": "Sem permissao"}), 403
    if "arquivo" not in request.files:
        return jsonify({"erro": "Envie um arquivo"}), 400
    arquivo = request.files["arquivo"]
    nome = request.form.get("nome", "").strip() or arquivo.filename.rsplit(".", 1)[0].replace("-", " ").replace("_", " ")
    categoria = request.form.get("categoria", "").strip()
    tipo_audio = request.form.get("tipo_audio", "musica").strip()  # musica ou efeito
    if tipo_audio not in ("musica", "efeito"):
        tipo_audio = "musica"
    if not arquivo.filename.lower().endswith((".mp3", ".wav", ".m4a", ".ogg")):
        return jsonify({"erro": "Formato inválido"}), 400
    # Auto-categorizar se não especificou
    if not categoria or categoria == "auto":
        try:
            api_key = current_user.get_api_key()
            if api_key:
                headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
                if tipo_audio == "efeito":
                    cats_disponiveis = "transicao, impacto, whoosh, notificacao, ambiente, risada, aplausos, suspense, magico, tecnologia, natureza, ui, cinematico, geral"
                    prompt_sys = f"""Analise o nome deste arquivo de efeito sonoro e retorne TODAS as categorias que se encaixam.
Categorias disponíveis: {cats_disponiveis}

Retorne APENAS as categorias separadas por vírgula. Exemplo:
impacto, cinematico"""
                else:
                    cats_disponiveis = "epico, motivacional, suspense, infantil, calmo, alegre, triste, romantico, tecnologia, efeito, geral"
                    prompt_sys = f"""Analise o nome deste arquivo de música instrumental e retorne TODAS as categorias que se encaixam.
Categorias disponíveis: {cats_disponiveis}

Retorne APENAS as categorias separadas por vírgula. Exemplo:
epico, motivacional"""
                body = {"model": "gpt-4o-mini", "messages": [
                    {"role": "system", "content": prompt_sys},
                    {"role": "user", "content": f"Nome do arquivo: {arquivo.filename}"}
                ], "max_tokens": 30}
                r = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=body, timeout=10)
                if r.ok:
                    texto = r.json()["choices"][0]["message"]["content"].strip().lower()
                    if tipo_audio == "efeito":
                        categorias_validas = ["transicao","impacto","whoosh","notificacao","ambiente","risada","aplausos","suspense","magico","tecnologia","natureza","ui","cinematico","geral"]
                    else:
                        categorias_validas = ["epico","motivacional","suspense","infantil","calmo","alegre","triste","romantico","tecnologia","efeito","geral"]
                    cats = [c.strip() for c in texto.split(",") if c.strip() in categorias_validas]
                    if cats:
                        categoria = ",".join(cats)
        except: pass
        if not categoria:
            categoria = "geral"
    filename = f"{uuid.uuid4().hex[:8]}_{arquivo.filename}"
    filepath = os.path.join(MUSICAS_SISTEMA_FOLDER, filename)
    arquivo.save(filepath)
    conn = sqlite3.connect('instance/veo3.db')
    conn.execute("""CREATE TABLE IF NOT EXISTS musicas_sistema (
        id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT, categoria TEXT, path TEXT,
        tipo TEXT DEFAULT 'musica',
        criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    try: conn.execute("ALTER TABLE musicas_sistema ADD COLUMN tipo TEXT DEFAULT 'musica'")
    except: pass
    # Verificar duplicata pelo nome + tipo
    existente = conn.execute("SELECT id FROM musicas_sistema WHERE nome=? AND tipo=?", (nome, tipo_audio)).fetchone()
    if existente:
        os.remove(filepath)
        conn.close()
        return jsonify({"ok": False, "erro": "Já existe", "duplicada": True})
    conn.execute("INSERT INTO musicas_sistema (nome, categoria, path, tipo) VALUES (?,?,?,?)", (nome, categoria, filename, tipo_audio))
    conn.commit(); conn.close()
    return jsonify({"ok": True, "categoria": categoria})

@app.route("/admin/deletar_musica_sistema", methods=["POST"])
@login_required
def admin_deletar_musica_sistema():
    if not current_user.is_admin:
        return jsonify({"erro": "Sem permissao"}), 403
    musica_id = request.json.get("id")
    try:
        conn = sqlite3.connect('instance/veo3.db')
        row = conn.execute("SELECT path FROM musicas_sistema WHERE id=?", (musica_id,)).fetchone()
        if row:
            filepath = os.path.join(MUSICAS_SISTEMA_FOLDER, row[0])
            if os.path.exists(filepath):
                os.remove(filepath)
        conn.execute("DELETE FROM musicas_sistema WHERE id=?", (musica_id,))
        conn.commit(); conn.close()
        return jsonify({"ok": True})
    except:
        return jsonify({"erro": "Erro ao deletar"}), 500

# ── Rotas Storyboard ─────────────────────────────────────
@app.route("/dividir_roteiro", methods=["POST"])
@login_required
def dividir_roteiro_route():
    """Divide o roteiro em cenas sem gerar imagens — pra o usuário preencher do banco antes"""
    texto = request.form.get("texto", "").strip()
    if not texto:
        return jsonify({"erro": "Escreva o roteiro"}), 400

    # Cobrar 1 crédito pra dividir (grátis pra trial sem plano)
    if current_user.plano:
        if not current_user.gastar_creditos(1):
            return jsonify({"erro": "Créditos insuficientes. Necessário: 1 crédito."}), 400
        db.session.commit()
    # Registrar ação
    try:
        conn_a = sqlite3.connect('instance/veo3.db')
        conn_a.execute("CREATE TABLE IF NOT EXISTS user_acoes (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, acao TEXT, detalhe TEXT, criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        conn_a.execute("INSERT INTO user_acoes (user_id, acao, detalhe) VALUES (?, ?, ?)", (current_user.id, "dividir_roteiro", f"{len(texto)} chars"))
        conn_a.commit(); conn_a.close()
    except: pass

    estilo = request.form.get("estilo", "").strip()
    melhorar = request.form.get("melhorar_prompts", "false") == "true"
    tipo_video = request.form.get("tipo_video", "estatico").strip()

    if melhorar and current_user.get_provider() == "openai" and current_user.get_api_key():
        linhas = dividir_roteiro(texto, current_user.get_api_key(), tipo_video)
    else:
        linhas = [l.strip() for l in texto.replace(",", "\n").replace(".", "\n").split("\n") if l.strip()] or [texto.strip()]

    cenas = [{"index": i+1, "texto": l, "preenchida": False} for i, l in enumerate(linhas)]
    return jsonify({"cenas": cenas, "total": len(cenas)})

@app.route("/gerar_storyboard", methods=["POST"])
@login_required
def gerar_storyboard_route():
    # Rate limit: max 3 gerações por minuto por usuário
    if rate_limit_check(f"gerar_{current_user.id}", max_requests=3, window=60):
        return jsonify({"erro": "Aguarde um momento antes de gerar novamente."}), 429
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

    # Salvar personagem de referência se enviado
    personagem_path = ""
    if "personagem" in request.files and request.files["personagem"].filename:
        personagem_file = request.files["personagem"]
        personagem_path = os.path.join(UPLOAD_FOLDER, f"personagem_{current_user.id}_{uuid.uuid4().hex[:8]}.png")
        personagem_file.save(personagem_path)

    tipo_video = request.form.get("tipo_video", "estatico").strip()

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "aguardando", "progresso": "Na fila...", "total": 0, "atual": 0}
    thread = threading.Thread(target=gerar_storyboard, args=(job_id, current_user.id, texto, estilo, melhorar, False, cenas_preenchidas, direcao_criativa, formato, personagem_path, tipo_video))
    thread.daemon = True
    thread.start()
    return jsonify({"job_id": job_id})

@app.route("/storyboard_img/<sb_id>/<filename>")
@login_required
def storyboard_img(sb_id, filename):
    if ".." in filename or filename.startswith("/"):
        return jsonify({"erro": "Acesso negado"}), 403
    filepath = os.path.join(STORYBOARD_FOLDER, sb_id, filename)
    if not os.path.exists(filepath):
        return jsonify({"erro": "Arquivo não encontrado"}), 404
    return send_file(filepath)

@app.route("/rascunhos")
@login_required
def listar_rascunhos():
    """Lista storyboards salvos do usuário — otimizado"""
    rascunhos = []
    if os.path.exists(STORYBOARD_FOLDER):
        # Listar diretórios e ordenar por data de modificação (mais recentes primeiro)
        dirs = []
        try:
            for sb_id in os.listdir(STORYBOARD_FOLDER):
                sb_path = os.path.join(STORYBOARD_FOLDER, sb_id, "storyboard.json")
                if os.path.exists(sb_path):
                    dirs.append((sb_id, os.path.getmtime(sb_path)))
        except: pass
        dirs.sort(key=lambda x: x[1], reverse=True)
        # Só ler os 20 mais recentes
        for sb_id, mtime in dirs[:20]:
            sb_path = os.path.join(STORYBOARD_FOLDER, sb_id, "storyboard.json")
            try:
                with open(sb_path) as f:
                    sb_data = json.load(f)
                blocos = sb_data.get("blocos", [])
                if not blocos:
                    continue
                data = datetime.fromtimestamp(mtime).strftime('%d/%m/%Y %H:%M')
                rascunhos.append({
                    "sb_id": sb_id,
                    "total_cenas": len(blocos),
                    "estilo": sb_data.get("estilo", ""),
                    "tipo_video": sb_data.get("tipo_video", "animado"),
                    "data": data,
                    "preview": blocos[0].get("texto", "")[:60],
                    "thumb": blocos[0].get("img", "")
                })
            except: continue
    return jsonify({"rascunhos": rascunhos})

@app.route("/carregar_rascunho/<sb_id>")
@login_required
def carregar_rascunho(sb_id):
    """Carrega um storyboard salvo"""
    sb_path = os.path.join(STORYBOARD_FOLDER, sb_id, "storyboard.json")
    if not os.path.exists(sb_path):
        return jsonify({"erro": "Rascunho não encontrado"}), 404
    with open(sb_path) as f:
        sb_data = json.load(f)
    return jsonify({"sb_id": sb_id, "blocos": sb_data.get("blocos", []), "estilo": sb_data.get("estilo", ""), "tipo_video": sb_data.get("tipo_video", "animado")})

@app.route("/regerar_cena", methods=["POST"])
@login_required
def regerar_cena():
    if rate_limit_check(f"regerar_{current_user.id}", max_requests=5, window=60):
        return jsonify({"erro": "Aguarde antes de regerar novamente."}), 429
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
    img_file = request.files["imagem"]
    img_file.seek(0, 2)
    if img_file.tell() > 20 * 1024 * 1024:
        return jsonify({"erro": "Imagem muito grande. Máximo 20MB."}), 400
    img_file.seek(0)
    sb_dir = os.path.join(STORYBOARD_FOLDER, sb_id)
    sb_path = os.path.join(sb_dir, "storyboard.json")
    with open(sb_path) as f:
        sb_data = json.load(f)
    bloco = sb_data["blocos"][index - 1]
    img_path = os.path.join(sb_dir, bloco["img"])
    img_file.save(img_path)
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
    # Trial: bloquear finalização de vídeo
    if not current_user.plano and not current_user.is_admin:
        return jsonify({"erro": "Assine um plano para gerar vídeos completos. Suas 2 imagens de demonstração foram geradas com sucesso!"}), 403

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
    efeitos_sonoros = request.form.get("efeitos_sonoros", "false") == "true"
    musica_id = request.form.get("musica_id", "").strip()
    musica_path = ""
    if musica_id:
        if musica_id.startswith("jamendo:"):
            # Baixar música da Jamendo
            try:
                audio_url = musica_id[8:]  # remover "jamendo:"
                r_mus = requests.get(audio_url, timeout=60)
                if r_mus.ok:
                    musica_path = os.path.join(UPLOAD_FOLDER, f"jamendo_{uuid.uuid4().hex[:8]}.mp3")
                    with open(musica_path, "wb") as f:
                        f.write(r_mus.content)
            except: pass
        elif musica_id.startswith("sys_"):
            # Música do sistema
            try:
                sys_id = int(musica_id[4:])
                conn = sqlite3.connect('instance/veo3.db')
                row = conn.execute("SELECT path FROM musicas_sistema WHERE id=?", (sys_id,)).fetchone()
                conn.close()
                if row:
                    musica_path = os.path.join(MUSICAS_SISTEMA_FOLDER, row[0])
            except: pass
        else:
            try:
                conn = sqlite3.connect('instance/veo3.db')
                row = conn.execute("SELECT path FROM musicas WHERE id=? AND user_id=?", (int(musica_id), current_user.id)).fetchone()
                conn.close()
                if row and os.path.exists(row[0]):
                    musica_path = row[0]
            except: pass
    import sys
    sys.stderr.write(f"[ROTA] animar_ia={animar_ia}, efeitos_sonoros={efeitos_sonoros}, musica={musica_path or 'nenhuma'}\n")
    sys.stderr.flush()
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "aguardando", "progresso": "Na fila...", "total": 0, "atual": 0}
    thread = threading.Thread(target=finalizar_video, args=(job_id, current_user.id, sb_id, voice_id, modo_video, legenda_cfg, intervalo, animar_ia, musica_path, efeitos_sonoros))
    thread.daemon = True
    thread.start()
    return jsonify({"job_id": job_id})

@app.route("/status/<job_id>")
@login_required
def status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"erro": "Job nao encontrado"}), 404
    # Jobs em memória não têm user_id, mas são temporários e UUID aleatório
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

@app.route("/ver_video/<job_id>")
@login_required
def ver_video(job_id):
    """Serve o vídeo pra streaming inline (sem forçar download)"""
    criacao = Criacao.query.filter_by(job_id=job_id, user_id=current_user.id).first()
    job = jobs.get(job_id)
    video_path = criacao.video_path if criacao else (job.get("video") if job else None)
    if not video_path or not os.path.exists(video_path):
        return jsonify({"erro": "Video nao disponivel"}), 404
    return send_file(video_path, mimetype="video/mp4")

# ── Error Handlers ────────────────────────────────────────
@app.route("/favicon.ico")
def favicon():
    fav_path = os.path.join("static", "favicon.png")
    if os.path.exists(fav_path):
        return send_file(fav_path)
    # Gerar favicon dinamicamente
    try:
        from PIL import ImageDraw, ImageFont
        img = Image.new('RGBA', (64, 64), (11, 17, 32, 255))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle([4, 4, 60, 60], radius=12, fill=(37, 99, 235, 255))
        try: font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 36)
        except: font = ImageFont.load_default()
        draw.text((18, 10), "K", fill=(255, 255, 255, 255), font=font)
        os.makedirs("static", exist_ok=True)
        img.save(fav_path)
        img.close()
        return send_file(fav_path)
    except:
        return "", 204

@app.errorhandler(404)
def page_not_found(e):
    return '''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>404 — Klyonclaw Studio</title>
    <style>*{margin:0;padding:0;box-sizing:border-box}body{font-family:'Segoe UI',sans-serif;background:#0b1120;color:#e2e8f0;min-height:100vh;display:flex;align-items:center;justify-content:center;text-align:center;padding:20px}
    .c{max-width:400px}.n{font-size:5rem;font-weight:800;color:#4a9eff;margin-bottom:12px}.t{font-size:1.2rem;margin-bottom:8px}.s{color:#94a3b8;font-size:.9rem;margin-bottom:24px}
    a{display:inline-block;padding:12px 28px;background:#2563eb;color:#fff;border-radius:10px;text-decoration:none;font-weight:600}a:hover{background:#4a9eff}</style></head>
    <body><div class="c"><div class="n">404</div><div class="t">Página não encontrada</div><p class="s">A página que você procura não existe ou foi movida.</p><a href="/">Voltar ao início</a></div></body></html>''', 404

@app.errorhandler(500)
def internal_error(e):
    return '''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>500 — Klyonclaw Studio</title>
    <style>*{margin:0;padding:0;box-sizing:border-box}body{font-family:'Segoe UI',sans-serif;background:#0b1120;color:#e2e8f0;min-height:100vh;display:flex;align-items:center;justify-content:center;text-align:center;padding:20px}
    .c{max-width:400px}.n{font-size:5rem;font-weight:800;color:#ef4444;margin-bottom:12px}.t{font-size:1.2rem;margin-bottom:8px}.s{color:#94a3b8;font-size:.9rem;margin-bottom:24px}
    a{display:inline-block;padding:12px 28px;background:#2563eb;color:#fff;border-radius:10px;text-decoration:none;font-weight:600}a:hover{background:#4a9eff}</style></head>
    <body><div class="c"><div class="n">500</div><div class="t">Erro interno</div><p class="s">Algo deu errado. Tente novamente em alguns instantes.</p><a href="/">Voltar ao início</a></div></body></html>''', 500

# ── Cleanup automático ────────────────────────────────────
def limpar_arquivos_antigos():
    """Remove arquivos temporários com mais de 7 dias"""
    import time
    agora = time.time()
    limite = 7 * 24 * 3600  # 7 dias
    for pasta in [OUTPUT_FOLDER, STORYBOARD_FOLDER]:
        try:
            for item in os.listdir(pasta):
                caminho = os.path.join(pasta, item)
                if os.path.isfile(caminho) and (agora - os.path.getmtime(caminho)) > limite:
                    os.remove(caminho)
                elif os.path.isdir(caminho) and (agora - os.path.getmtime(caminho)) > limite:
                    shutil.rmtree(caminho, ignore_errors=True)
        except: pass

# Rodar cleanup na inicialização
try: limpar_arquivos_antigos()
except: pass

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        # Migrate: adicionar colunas novas se não existirem
        try:
            conn = sqlite3.connect('instance/veo3.db')
            try: conn.execute("ALTER TABLE user ADD COLUMN banco_ativo BOOLEAN DEFAULT 0")
            except: pass
            try: conn.execute("ALTER TABLE user ADD COLUMN audio_ativo BOOLEAN DEFAULT 0")
            except: pass
            conn.commit()
            conn.close()
        except: pass
        atualizar_branding_stripe()
    app.run(host="0.0.0.0", port=5000)
else:
    # Gunicorn: migrate automático
    with app.app_context():
        try:
            conn = sqlite3.connect('instance/veo3.db')
            try: conn.execute("ALTER TABLE user ADD COLUMN banco_ativo BOOLEAN DEFAULT 0")
            except: pass
            try: conn.execute("ALTER TABLE user ADD COLUMN audio_ativo BOOLEAN DEFAULT 0")
            except: pass
            conn.commit()
            conn.close()
        except: pass
    # Atualizar branding na Stripe (fora do try pra garantir execução)
    try:
        atualizar_branding_stripe()
    except: pass
