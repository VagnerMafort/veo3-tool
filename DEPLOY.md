# Deploy na VPS

## 1. Enviar os arquivos para a VPS

```bash
scp -r veo3-tool/ usuario@IP_DA_VPS:/var/www/veo3-tool
```

## 2. Na VPS — instalar dependências do sistema

```bash
sudo apt update
sudo apt install python3-pip python3-venv ffmpeg nginx -y
```

## 3. Criar ambiente virtual e instalar pacotes

```bash
cd /var/www/veo3-tool
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 4. Configurar o Nginx

```bash
# Substitua SEU_DOMINIO.com pelo seu domínio no arquivo nginx.conf
sudo cp nginx.conf /etc/nginx/sites-available/veo3
sudo ln -s /etc/nginx/sites-available/veo3 /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

## 5. Configurar o serviço systemd (roda automaticamente)

```bash
sudo cp veo3.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable veo3
sudo systemctl start veo3
```

## 6. SSL gratuito com Certbot (HTTPS)

```bash
sudo apt install certbot python3-certbot-nginx -y
sudo certbot --nginx -d SEU_DOMINIO.com -d www.SEU_DOMINIO.com
```

## Verificar se está rodando

```bash
sudo systemctl status veo3
```

## Ver logs em tempo real

```bash
sudo journalctl -u veo3 -f
```
