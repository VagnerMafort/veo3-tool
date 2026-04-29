#!/usr/bin/env python3
"""
Script para criar o produto 'Banco de Músicas e Efeitos Sonoros' na Stripe.
Rodar na VPS: python3 create_audio_price.py
O price_id gerado deve ser colocado na env AUDIO_ADDON_PRICE_ID
"""
import os
import stripe

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
if not stripe.api_key:
    print("❌ STRIPE_SECRET_KEY não encontrada no ambiente")
    exit(1)

print("Criando produto na Stripe...")

# Criar produto
product = stripe.Product.create(
    name="Banco de Músicas e Efeitos Sonoros",
    description="Acesso a músicas instrumentais e efeitos sonoros profissionais para seus vídeos. Inclui efeitos automáticos com IA.",
    metadata={"tipo": "addon", "key": "audio"}
)
print(f"✅ Produto criado: {product.id}")

# Criar price recorrente R$4,99/mês
price = stripe.Price.create(
    product=product.id,
    unit_amount=499,  # R$4,99 em centavos
    currency="brl",
    recurring={"interval": "month"},
    metadata={"tipo": "addon", "key": "audio"}
)
print(f"✅ Price criado: {price.id}")
print(f"\n📋 Adicione na VPS:")
print(f"   export AUDIO_ADDON_PRICE_ID={price.id}")
print(f"\n   Ou edite /etc/systemd/system/veo3.service e adicione:")
print(f"   Environment=AUDIO_ADDON_PRICE_ID={price.id}")
print(f"\n   Depois: sudo systemctl daemon-reload && sudo systemctl restart veo3")
