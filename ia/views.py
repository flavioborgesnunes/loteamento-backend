import tempfile

import fitz
import requests
import unidecode
from bs4 import BeautifulSoup
from decouple import config
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnableSequence
from langchain_openai import ChatOpenAI
from rest_framework.response import Response
from rest_framework.views import APIView

SERPER_API_KEY = config("SERPER_API_KEY")
OPENAI_API_KEY = config("OPENAI_API_KEY")

KEYWORDS = [
    "largura mínima", "largura", "calçada", "m²", "m2",
    "dimensão mínima", "lote mínimo", "tamanho mínimo",
    "parcelamento do solo", "recuo", "metros", "mínimo de",
    "área mínima", "curso d’água", "app"
]


class LegalAutoFillView(APIView):
    def post(self, request):
        cidade = request.data.get("cidade")
        if not cidade:
            return Response({"erro": "Cidade é obrigatória."}, status=400)

        # 🔎 Consulta IBGE
        try:
            ibge_resp = requests.get(
                "https://servicodados.ibge.gov.br/api/v1/localidades/municipios", timeout=10)
            municipios = ibge_resp.json()
            cidade_normalizada = unidecode.unidecode(cidade.strip().lower())
            municipio = next(
                (m for m in municipios if unidecode.unidecode(
                    m["nome"].lower()) == cidade_normalizada),
                None
            )
        except:
            municipio = None

        codigo_ibge = municipio["id"] if municipio else None
        estado = municipio["microrregiao"]["mesorregiao"]["UF"]["nome"] if municipio else ""

        # 🔍 Perguntas fixas para a IA
        perguntas = {
            "campo-area-minima": f"No município de {cidade}, qual é a área mínima exigida para lotes urbanos residenciais segundo o plano diretor ou legislação de parcelamento do solo?",
            "campo-largura-calcada": f"Segundo a legislação de {cidade}, qual é a largura mínima exigida para calçadas em áreas urbanas?",
            "recuo_frontal": f"De acordo com a legislação urbanística de {cidade}, qual é o recuo frontal mínimo obrigatório para construções residenciais?",
            "recuo_lateral": f"Qual é o recuo lateral mínimo exigido para edificações em lotes urbanos segundo o código de obras ou zoneamento de {cidade}?",
        }

        respostas = {}
        logs_respostas = []

        # 💬 Consulta a IA para cada pergunta
        for campo, pergunta in perguntas.items():
            resultado = consultar_ia(cidade, pergunta)
            respostas[campo] = resultado or "sem informação"
            logs_respostas.append([pergunta, resultado])

        # 🧾 Resposta completa
        return Response({
            "cidade": cidade,
            "estado": estado,
            "codigo_ibge": codigo_ibge,
            **respostas,
            "resposta_ia": logs_respostas
        })


def consultar_ia(cidade, pergunta):
    # 🔎 Busca links relevantes no Google (via Serper.dev)
    search_query = f"{cidade} plano diretor largura mínima site:.gov.br OR site:leismunicipais.com.br OR filetype:pdf"
    try:
        search_response = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": SERPER_API_KEY,
                     "Content-Type": "application/json"},
            json={"q": search_query},
            timeout=15
        )
        links = search_response.json().get("organic", [])
    except:
        links = []

    # 📄 Coleta conteúdo dos documentos encontrados
    textos_relevantes = []
    for item in links[:3]:
        url = item.get("link")
        try:
            if url.endswith(".pdf"):
                r = requests.get(url, timeout=15)
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
                    tmp.write(r.content)
                    tmp.flush()
                    doc = fitz.open(tmp.name)
                    texto = "\n".join(p.get_text() for p in doc)
                    textos_relevantes += [
                        f"PDF: {url}\n{p}" for p in texto.split('\n')
                        if any(k in p.lower() for k in KEYWORDS)
                    ]
            else:
                html = requests.get(url, timeout=15).text
                soup = BeautifulSoup(html, "html.parser")
                texto = soup.get_text(separator="\n")
                textos_relevantes += [
                    f"HTML: {url}\n{p}" for p in texto.split('\n')
                    if any(k in p.lower() for k in KEYWORDS)
                ]
        except:
            continue

    if not textos_relevantes:
        return "sem informação"

    # 📚 Limita base de conhecimento para o prompt
    base_conhecimento = "\n\n".join(textos_relevantes)[:8000]

    # 🤖 Prompt da IA
    prompt = PromptTemplate.from_template("""
Você é um especialista em legislação urbana. Com base exclusivamente nos trechos abaixo extraídos de documentos legais e sites oficiais, responda à pergunta do usuário. Cite valores numéricos quando houver.

{conteudo}

Pergunta: {pergunta}
Resposta:
""")

    llm = ChatOpenAI(model="gpt-4o", api_key=OPENAI_API_KEY)
    chain: RunnableSequence = prompt | llm

    resposta_raw = chain.invoke({
        "conteudo": base_conhecimento,
        "pergunta": pergunta
    })

    # ✅ Retorno limpo
    if hasattr(resposta_raw, "content"):
        return resposta_raw.content
    elif isinstance(resposta_raw, dict) and "content" in resposta_raw:
        return resposta_raw["content"]
    elif isinstance(resposta_raw, str):
        return resposta_raw
    elif isinstance(resposta_raw, list):
        return "\n".join(str(p) for p in resposta_raw)
    else:
        return str(resposta_raw)
