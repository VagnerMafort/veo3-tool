#!/bin/bash
set -e

DOMAIN="automacao.grupomafort.com"
DIR="/var/www/veo3-tool"

echo "======================================"
echo " Instalando Veo3 Prompt Generator"
echo "======================================"

# 1. Dependências do sistema
echo "[1/7] Instalando dependências do sistema..."
apt update -y && apt install python3-pip python3-venv ffmpeg nginx certbot python3-certbot-nginx -y

# 2. Criar estrutura de pastas
echo "[2/7] Criando estrutura de pastas..."
mkdir -p $DIR/templates $DIR/uploads $DIR/outputs
cd $DIR

# 3. Criar app.py
echo "[3/7] Criando app.py..."
cat > app.py << 'PYEOF'
from flask import Flask, request, render_template, send_file, jsonify
import whisper, math, os, uuid, threading
from pydub import AudioSegment

app = Flask(__name__)

UPLOAD_FOLDER = "uploads"
OUTPUT_FOLDER = "outputs"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

jobs = {}
model = whisper.load_model("small")

def processar_audio(job_id, caminho, nome_original):
    try:
        jobs[job_id] = {"status": "processando", "progresso": "Transcrevendo áudio..."}
        resultado = model.transcribe(caminho, word_timestamps=True, fp16=False)
        jobs[job_id]["progresso"] = "Gerando blocos de 8s..."
        audio_info = AudioSegment.from_file(caminho)
        duracao_total = len(audio_info) / 1000
        total_blocos = math.ceil(duracao_total / 8)
        gavetas = {i: [] for i in range(total_blocos)}
        for seg in resultado['segments']:
            for palavra in seg['words']:
                idx = int(palavra['start'] // 8)
                if idx in gavetas:
                    gavetas[idx].append(palavra['word'].strip())
        linhas, ultimo = [], "Background scene"
        for i in range(total_blocos):
            texto = " ".join(gavetas[i]) or ultimo
            ultimo = texto
            linhas.append(f"Bloco {i+1:03d} [{i*8:03d}s - {(i+1)*8:03d}s]: {texto}")
        saida = os.path.join(OUTPUT_FOLDER, f"{job_id}.txt")
        with open(saida, "w", encoding="utf-8") as f:
            f.write("MAPA DE PROMPTS PARA VEO3 - DIVISÃO RÍGIDA 8s\n")
            f.write(f"Arquivo: {nome_original}\n")
            f.write("-" * 50 + "\n")
            f.write("\n".join(linhas))
        os.remove(caminho)
        jobs[job_id] = {"status": "pronto", "progresso": "Concluído!", "arquivo": saida, "nome": f"PROMPTS_VEO3_{nome_original}.txt", "total_blocos": total_blocos}
    except Exception as e:
        jobs[job_id] = {"status": "erro", "progresso": str(e)}

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/upload", methods=["POST"])
def upload():
    if "audio" not in request.files:
        return jsonify({"erro": "Nenhum arquivo enviado"}), 400
    audio = request.files["audio"]
    if not audio.filename.endswith((".mp3", ".wav", ".m4a", ".ogg")):
        return jsonify({"erro": "Formato inválido. Use MP3, WAV, M4A ou OGG"}), 400
    job_id = str(uuid.uuid4())
    caminho = os.path.join(UPLOAD_FOLDER, f"{job_id}_{audio.filename}")
    audio.save(caminho)
    jobs[job_id] = {"status": "aguardando", "progresso": "Na fila..."}
    thread = threading.Thread(target=processar_audio, args=(job_id, caminho, audio.filename))
    thread.daemon = True
    thread.start()
    return jsonify({"job_id": job_id})

@app.route("/status/<job_id>")
def status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"erro": "Job não encontrado"}), 404
    return jsonify(job)

@app.route("/download/<job_id>")
def download(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "pronto":
        return jsonify({"erro": "Arquivo não disponível"}), 404
    return send_file(job["arquivo"], as_attachment=True, download_name=job["nome"])

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
PYEOF

# 4. Criar index.html
echo "[4/7] Criando interface web..."
cat > templates/index.html << 'HTMLEOF'
<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Gerador de Prompts Veo3</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:'Segoe UI',sans-serif;background:#0f0f0f;color:#f0f0f0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
    .card{background:#1a1a1a;border:1px solid #2a2a2a;border-radius:16px;padding:36px 28px;width:100%;max-width:480px;box-shadow:0 8px 40px rgba(0,0,0,.5)}
    .logo{text-align:center;margin-bottom:28px}.logo h1{font-size:1.6rem;font-weight:700;color:#fff}.logo span{color:#7c3aed}.logo p{font-size:.85rem;color:#888;margin-top:6px}
    .drop-area{border:2px dashed #3a3a3a;border-radius:12px;padding:36px 20px;text-align:center;cursor:pointer;transition:border-color .2s,background .2s;position:relative}
    .drop-area:hover,.drop-area.dragover{border-color:#7c3aed;background:#1f1535}
    .drop-area input[type=file]{position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%}
    .drop-icon{font-size:2.5rem;margin-bottom:10px}.drop-area p{color:#aaa;font-size:.9rem}.drop-area .formatos{font-size:.75rem;color:#666;margin-top:6px}
    .file-name{margin-top:12px;font-size:.85rem;color:#7c3aed;text-align:center;min-height:20px}
    .btn{width:100%;margin-top:20px;padding:14px;background:#7c3aed;color:#fff;border:none;border-radius:10px;font-size:1rem;font-weight:600;cursor:pointer;transition:background .2s,transform .1s}
    .btn:hover{background:#6d28d9}.btn:active{transform:scale(.98)}.btn:disabled{background:#3a3a3a;color:#666;cursor:not-allowed}
    .status-box{display:none;margin-top:24px;background:#111;border-radius:10px;padding:18px;text-align:center}
    .spinner{width:36px;height:36px;border:3px solid #2a2a2a;border-top-color:#7c3aed;border-radius:50%;animation:spin .8s linear infinite;margin:0 auto 12px}
    @keyframes spin{to{transform:rotate(360deg)}}.status-text{font-size:.9rem;color:#ccc}
    .resultado{display:none;margin-top:24px;text-align:center}.resultado .check{font-size:2.5rem;margin-bottom:8px}.resultado p{color:#aaa;font-size:.85rem;margin-bottom:16px}
    .btn-download{display:inline-block;padding:12px 28px;background:#059669;color:#fff;border-radius:10px;font-weight:600;text-decoration:none;font-size:.95rem;transition:background .2s}
    .btn-download:hover{background:#047857}
    .btn-novo{display:block;margin-top:12px;background:none;border:1px solid #3a3a3a;color:#888;padding:10px;border-radius:10px;cursor:pointer;font-size:.85rem;width:100%;transition:border-color .2s}
    .btn-novo:hover{border-color:#7c3aed;color:#ccc}
    .erro-box{display:none;margin-top:20px;background:#2a0a0a;border:1px solid #7f1d1d;border-radius:10px;padding:14px;color:#fca5a5;font-size:.85rem;text-align:center}
  </style>
</head>
<body>
  <div class="card">
    <div class="logo">
      <h1>Prompts <span>Veo3</span></h1>
      <p>Transcrição automática em blocos de 8 segundos</p>
    </div>
    <div class="drop-area" id="dropArea">
      <input type="file" id="fileInput" accept=".mp3,.wav,.m4a,.ogg" />
      <div class="drop-icon">🎵</div>
      <p>Toque para selecionar o áudio</p>
      <p class="formatos">MP3 · WAV · M4A · OGG</p>
    </div>
    <div class="file-name" id="fileName"></div>
    <button class="btn" id="btnEnviar" disabled>Gerar Prompts</button>
    <div class="status-box" id="statusBox">
      <div class="spinner"></div>
      <div class="status-text" id="statusText">Processando...</div>
    </div>
    <div class="resultado" id="resultado">
      <div class="check">✅</div>
      <p id="resultadoInfo">Prompts gerados com sucesso!</p>
      <a href="#" id="btnDownload" class="btn-download">⬇ Baixar .txt</a>
      <button class="btn-novo" id="btnNovo">Processar outro áudio</button>
    </div>
    <div class="erro-box" id="erroBox"></div>
  </div>
  <script>
    const fileInput=document.getElementById('fileInput'),fileName=document.getElementById('fileName'),btnEnviar=document.getElementById('btnEnviar'),statusBox=document.getElementById('statusBox'),statusText=document.getElementById('statusText'),resultado=document.getElementById('resultado'),resultadoInfo=document.getElementById('resultadoInfo'),btnDownload=document.getElementById('btnDownload'),btnNovo=document.getElementById('btnNovo'),erroBox=document.getElementById('erroBox'),dropArea=document.getElementById('dropArea');
    let arquivoSelecionado=null;
    fileInput.addEventListener('change',()=>{arquivoSelecionado=fileInput.files[0];if(arquivoSelecionado){fileName.textContent=arquivoSelecionado.name;btnEnviar.disabled=false;}});
    dropArea.addEventListener('dragover',e=>{e.preventDefault();dropArea.classList.add('dragover');});
    dropArea.addEventListener('dragleave',()=>dropArea.classList.remove('dragover'));
    dropArea.addEventListener('drop',e=>{e.preventDefault();dropArea.classList.remove('dragover');const file=e.dataTransfer.files[0];if(file){arquivoSelecionado=file;fileName.textContent=file.name;btnEnviar.disabled=false;}});
    btnEnviar.addEventListener('click',async()=>{if(!arquivoSelecionado)return;erroBox.style.display='none';btnEnviar.disabled=true;statusBox.style.display='block';statusText.textContent='Enviando arquivo...';const formData=new FormData();formData.append('audio',arquivoSelecionado);try{const res=await fetch('/upload',{method:'POST',body:formData});const data=await res.json();if(data.erro)throw new Error(data.erro);verificarStatus(data.job_id);}catch(e){mostrarErro(e.message);}});
    function verificarStatus(jobId){const intervalo=setInterval(async()=>{try{const res=await fetch(`/status/${jobId}`);const data=await res.json();statusText.textContent=data.progresso||'Processando...';if(data.status==='pronto'){clearInterval(intervalo);statusBox.style.display='none';resultadoInfo.textContent=`${data.total_blocos} blocos gerados com sucesso!`;btnDownload.href=`/download/${jobId}`;resultado.style.display='block';}if(data.status==='erro'){clearInterval(intervalo);statusBox.style.display='none';mostrarErro(data.progresso);}}catch(e){clearInterval(intervalo);mostrarErro('Erro de conexão.');}},2000);}
    function mostrarErro(msg){erroBox.textContent='❌ '+msg;erroBox.style.display='block';btnEnviar.disabled=false;}
    btnNovo.addEventListener('click',()=>{resultado.style.display='none';erroBox.style.display='none';fileName.textContent='';fileInput.value='';arquivoSelecionado=null;btnEnviar.disabled=true;});
  </script>
</body>
</html>
HTMLEOF

# 5. Criar requirements.txt
cat > requirements.txt << 'EOF'
flask
pydub
openai-whisper
gunicorn
requests
EOF

# 6. Ambiente virtual e pacotes Python
echo "[5/7] Instalando pacotes Python (pode demorar 5-10 min)..."
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q

# 7. Nginx
echo "[6/7] Configurando Nginx..."
cat > /etc/nginx/sites-available/veo3 << EOF
server {
    listen 80;
    server_name $DOMAIN;
    client_max_body_size 200M;
    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }
}
EOF

rm -f /etc/nginx/sites-enabled/default
ln -sf /etc/nginx/sites-available/veo3 /etc/nginx/sites-enabled/veo3
nginx -t && systemctl reload nginx

# 8. Serviço systemd
echo "[7/7] Configurando serviço..."
cat > /etc/systemd/system/veo3.service << EOF
[Unit]
Description=Veo3 Prompt Generator
After=network.target

[Service]
User=root
WorkingDirectory=$DIR
ExecStart=$DIR/venv/bin/gunicorn --workers 2 --bind 0.0.0.0:5000 app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable veo3
systemctl start veo3

echo ""
echo "======================================"
echo " Instalação concluída!"
echo " Agora rode o SSL:"
echo " certbot --nginx -d $DOMAIN"
echo "======================================"
