"""Sistema de Análise de Dados: arquivos, regex e exceções.

Lê um arquivo CSV com e-mail, CPF, telefone e data de nascimento, valida
cada campo com expressões regulares (módulo re) e regras de negócio, e
gera um relatório com os registros válidos, os inválidos e estatísticas.

Uso:
    python3 analise_dados.py [arquivo.csv] [-o relatorio.txt]
                             [--data-referencia dd/mm/aaaa]
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from functools import partial

ARQUIVO_PADRAO = "dados.csv"
RELATORIO_PADRAO = "relatorio.txt"
COLUNAS_OBRIGATORIAS = ("email", "cpf", "telefone", "data_nascimento")
IDADE_MAXIMA = 120
LARGURA_RELATORIO = 70

# ---------------------------------------------------------------------
# Expressões regulares
# ---------------------------------------------------------------------
# re.ASCII faz \d e \w aceitarem só 0-9 e A-Z/a-z/_ (sem isso, \d também
# casaria dígitos de outros alfabetos, como os árabes).
#   ^ e $   : início e fim do texto (o valor inteiro deve casar)
#   \d      : um dígito           \w : letra, dígito ou "_"
#   \s      : espaço em branco    \. : o ponto literal (sem "\" é "qualquer")
#   ?  *  + : 0 ou 1 / 0 ou mais / 1 ou mais     {n} {n,} : repetições
#   [ ]     : conjunto de caracteres   ( | ) : grupo e alternativa

# parte local (letras, números, _ + -, pontos sem repetir) + @ + domínio
# com rótulos separados por ponto e um final de 2+ letras (.com, .com.br)
_ROTULO_DOMINIO = r"[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?"
PADRAO_EMAIL = re.compile(
    r"^[\w+-]+(\.[\w+-]+)*"
    r"@" + _ROTULO_DOMINIO + r"(\." + _ROTULO_DOMINIO + r")*"
    r"\.[A-Za-z]{2,}$",
    re.ASCII,
)

# 000.000.000-00 completo OU 11 dígitos seguidos (sem formatos misturados)
PADRAO_CPF = re.compile(r"^(\d{3}\.\d{3}\.\d{3}-\d{2}|\d{11})$", re.ASCII)

# [+55] [(DD) | DD] [9XXXX | XXXX][-]XXXX   ex.: (11) 91234-5678
PADRAO_TELEFONE = re.compile(
    r"^(\+55\s?)?(\([1-9]\d\)\s?|[1-9]\d\s?)(9\d{4}|\d{4})-?\d{4}$",
    re.ASCII,
)

# dd/mm/aaaa com dia 01-31 e mês 01-12 (datas como 31/04 passam aqui e
# são barradas depois, na conversão, porque o calendário não as aceita)
PADRAO_DATA = re.compile(
    r"^(0[1-9]|[12]\d|3[01])/(0[1-9]|1[0-2])/\d{4}$", re.ASCII
)


# ---------------------------------------------------------------------
# Exceções personalizadas (regras de negócio)
# ---------------------------------------------------------------------
class ValidacaoError(Exception):
    """Base das falhas de validação de um campo de um registro."""

    def __init__(self, campo, valor, motivo):
        super().__init__(motivo)
        self.campo = campo
        self.valor = valor
        self.motivo = motivo


class FormatoInvalidoError(ValidacaoError):
    """O valor não segue o formato esperado (falha no regex/data)."""


class DocumentoInvalidoError(ValidacaoError):
    """O formato está certo, mas os dígitos verificadores do CPF não."""


class IdadeInvalidaError(ValidacaoError):
    """A data é válida, mas a idade quebra a regra de negócio."""


# ---------------------------------------------------------------------
# Estruturas de dados
# ---------------------------------------------------------------------
@dataclass
class RegistroValido:
    linha: int
    nome: str
    email: str
    cpf: str
    telefone: str
    nascimento: date
    idade: int


@dataclass
class RegistroInvalido:
    linha: int
    nome: str
    erros: list


@dataclass
class ResultadoAnalise:
    arquivo: str
    data_referencia: date
    validos: list
    invalidos: list


# ---------------------------------------------------------------------
# Validadores de campo: devolvem o valor normalizado ou levantam erro
# ---------------------------------------------------------------------
def exigir_formato(campo, texto, padrao, esperado):
    """Levanta FormatoInvalidoError se o texto não casar com o padrão."""
    if texto == "":
        raise FormatoInvalidoError(campo, texto, "campo obrigatório vazio")
    # fullmatch exige que o texto INTEIRO case: sozinho, o "$" também
    # aceitaria uma quebra de linha sobrando no final ("a@b.com\n")
    if padrao.fullmatch(texto) is None:
        raise FormatoInvalidoError(
            campo, texto, f"formato inválido, esperado {esperado}"
        )


def validar_email(texto):
    exigir_formato("email", texto, PADRAO_EMAIL, "nome@dominio.com")
    if len(texto) > 254:
        raise FormatoInvalidoError("email", texto, "e-mail longo demais")
    return texto.lower()


def digitos_do_cpf_conferem(numeros):
    """Confere os dois dígitos verificadores de um CPF (11 dígitos)."""
    if numeros == numeros[0] * 11:  # 111.111.111-11 etc. são inválidos
        return False
    for posicao in (9, 10):
        pesos = range(posicao + 1, 1, -1)  # 10..2 e depois 11..2
        soma = sum(int(d) * peso for d, peso in zip(numeros, pesos))
        digito = (soma * 10) % 11 % 10
        if digito != int(numeros[posicao]):
            return False
    return True


def validar_cpf(texto):
    exigir_formato("cpf", texto, PADRAO_CPF, "000.000.000-00 ou 11 dígitos")
    numeros = re.sub(r"\D", "", texto)  # \D = tudo que não é dígito
    if not digitos_do_cpf_conferem(numeros):
        raise DocumentoInvalidoError(
            "cpf", texto, "dígitos verificadores não conferem"
        )
    return f"{numeros[:3]}.{numeros[3:6]}.{numeros[6:9]}-{numeros[9:]}"


def validar_telefone(texto):
    exigir_formato(
        "telefone", texto, PADRAO_TELEFONE, "(11) 91234-5678 ou 1132345678"
    )
    return texto


def converter_data(texto):
    """Converte 'dd/mm/aaaa' em date. Levanta ValueError se não existir."""
    return datetime.strptime(texto, "%d/%m/%Y").date()


def calcular_idade(nascimento, hoje):
    """Idade completa em anos na data 'hoje'."""
    fez_aniversario = (hoje.month, hoje.day) >= (
        nascimento.month,
        nascimento.day,
    )
    return hoje.year - nascimento.year - (0 if fez_aniversario else 1)


def verificar_idade(nascimento, hoje):
    """Regra de negócio: nascimento no passado e idade até IDADE_MAXIMA."""
    texto = f"{nascimento:%d/%m/%Y}"
    if nascimento > hoje:
        raise IdadeInvalidaError(
            "data_nascimento", texto, "data de nascimento no futuro"
        )
    idade = calcular_idade(nascimento, hoje)
    if idade > IDADE_MAXIMA:
        raise IdadeInvalidaError(
            "data_nascimento",
            texto,
            f"idade de {idade} anos acima do máximo ({IDADE_MAXIMA})",
        )


def validar_data_nascimento(texto, hoje):
    exigir_formato("data_nascimento", texto, PADRAO_DATA, "dd/mm/aaaa")
    try:
        nascimento = converter_data(texto)
    except ValueError as erro:  # 30/02/1992 passa no regex, mas não existe
        raise FormatoInvalidoError(
            "data_nascimento", texto, "data inexistente no calendário"
        ) from erro
    else:
        verificar_idade(nascimento, hoje)
        return nascimento


# ---------------------------------------------------------------------
# Leitura e análise do arquivo
# ---------------------------------------------------------------------
def detectar_delimitador(primeira_linha):
    """CSV brasileiro costuma usar ';' (Excel); senão, vírgula."""
    if primeira_linha.count(";") > primeira_linha.count(","):
        return ";"
    return ","


def verificar_colunas(colunas):
    """Levanta KeyError listando as colunas obrigatórias que faltam."""
    ausentes = [c for c in COLUNAS_OBRIGATORIAS if c not in colunas]
    if ausentes:
        raise KeyError(", ".join(ausentes))


def validar_registro(linha, numero_linha, hoje):
    """Valida todos os campos; devolve RegistroValido ou RegistroInvalido."""
    validadores = {
        "email": validar_email,
        "cpf": validar_cpf,
        "telefone": validar_telefone,
        "data_nascimento": partial(validar_data_nascimento, hoje=hoje),
    }
    nome = (linha.get("nome") or "").strip() or "(sem nome)"
    valores = {}
    erros = []
    for campo, validador in validadores.items():
        texto = (linha[campo] or "").strip()  # KeyError se faltar a coluna
        try:
            valores[campo] = validador(texto)
        except ValidacaoError as erro:
            erros.append(erro)

    if erros:
        return RegistroInvalido(numero_linha, nome, erros)
    nascimento = valores["data_nascimento"]
    return RegistroValido(
        linha=numero_linha,
        nome=nome,
        email=valores["email"],
        cpf=valores["cpf"],
        telefone=valores["telefone"],
        nascimento=nascimento,
        idade=calcular_idade(nascimento, hoje),
    )


def analisar_arquivo(caminho, hoje=None):
    """Lê o CSV e separa os registros válidos dos inválidos."""
    hoje = hoje or date.today()
    validos = []
    invalidos = []
    # utf-8-sig ignora o BOM que o Excel costuma gravar no início
    with open(caminho, newline="", encoding="utf-8-sig") as arquivo:
        delimitador = detectar_delimitador(arquivo.readline())
        arquivo.seek(0)
        leitor = csv.DictReader(arquivo, delimiter=delimitador)
        colunas = [c.strip().lower() for c in (leitor.fieldnames or [])]
        leitor.fieldnames = colunas
        verificar_colunas(colunas)
        for linha in leitor:
            registro = validar_registro(linha, leitor.line_num, hoje)
            if isinstance(registro, RegistroValido):
                validos.append(registro)
            else:
                invalidos.append(registro)
    return ResultadoAnalise(str(caminho), hoje, validos, invalidos)


# ---------------------------------------------------------------------
# Relatório (f-strings)
# ---------------------------------------------------------------------
def percentual(parte, total):
    return 100 * parte / total if total else 0.0


def gerar_relatorio(resultado):
    """Monta o texto do relatório final."""
    validos = resultado.validos
    invalidos = resultado.invalidos
    total = len(validos) + len(invalidos)
    linha_dupla = "=" * LARGURA_RELATORIO
    titulo = "RELATÓRIO DE ANÁLISE DE DADOS"

    texto = [
        linha_dupla,
        f"{titulo:^{LARGURA_RELATORIO}}",
        linha_dupla,
        f"Arquivo analisado  : {resultado.arquivo}",
        f"Data de referência : {resultado.data_referencia:%d/%m/%Y}",
        "",
        "RESUMO",
        f"  Total de registros : {total}",
        f"  Válidos            : {len(validos)} "
        f"({percentual(len(validos), total):.1f}%)",
        f"  Inválidos          : {len(invalidos)} "
        f"({percentual(len(invalidos), total):.1f}%)",
        "",
        f"REGISTROS VÁLIDOS ({len(validos)})",
    ]
    if not validos:
        texto.append("  (nenhum)")
    for reg in validos:
        texto.append(f"  [linha {reg.linha}] {reg.nome} ({reg.idade} anos)")
        texto.append(
            f"      {reg.email} | {reg.cpf} | {reg.telefone} | "
            f"{reg.nascimento:%d/%m/%Y}"
        )

    texto += ["", f"REGISTROS INVÁLIDOS ({len(invalidos)})"]
    if not invalidos:
        texto.append("  (nenhum)")
    for reg in invalidos:
        texto.append(f"  [linha {reg.linha}] {reg.nome}")
        for erro in reg.erros:
            texto.append(
                f"      - {erro.campo}: {erro.motivo} (valor: {erro.valor!r})"
            )

    erros_por_campo = Counter(
        erro.campo for reg in invalidos for erro in reg.erros
    )
    texto += ["", "ESTATÍSTICAS", "  Erros por campo:"]
    for campo in COLUNAS_OBRIGATORIAS:
        texto.append(f"    {campo:<16}: {erros_por_campo[campo]}")
    if validos:
        idade_media = sum(reg.idade for reg in validos) / len(validos)
        mais_novo = min(validos, key=lambda reg: reg.idade)
        mais_velho = max(validos, key=lambda reg: reg.idade)
        texto += [
            f"  Idade média (válidos): {idade_media:.1f} anos",
            f"  Mais novo : {mais_novo.nome} ({mais_novo.idade} anos)",
            f"  Mais velho: {mais_velho.nome} ({mais_velho.idade} anos)",
        ]
    texto.append(linha_dupla)
    return "\n".join(texto)


def salvar_relatorio(texto, caminho):
    """Grava o relatório em arquivo. Devolve True se conseguiu."""
    try:
        with open(caminho, "w", encoding="utf-8") as arquivo:
            arquivo.write(texto + "\n")
    except OSError as erro:
        print(f"Aviso: não foi possível salvar '{caminho}': {erro}",
              file=sys.stderr)
        return False
    else:
        return True


# ---------------------------------------------------------------------
# Linha de comando
# ---------------------------------------------------------------------
def data_da_linha_de_comando(texto):
    """Converte o argumento --data-referencia (trata ValueError)."""
    try:
        return converter_data(texto)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"data inválida: {texto!r} (use dd/mm/aaaa)"
        ) from None


def criar_parser():
    parser = argparse.ArgumentParser(
        description="Valida e-mail, CPF, telefone e data de um arquivo CSV."
    )
    parser.add_argument(
        "arquivo",
        nargs="?",
        default=ARQUIVO_PADRAO,
        help=f"arquivo CSV a analisar (padrão: {ARQUIVO_PADRAO})",
    )
    parser.add_argument(
        "-o",
        "--saida",
        default=RELATORIO_PADRAO,
        help=f"arquivo do relatório (padrão: {RELATORIO_PADRAO})",
    )
    parser.add_argument(
        "--data-referencia",
        type=data_da_linha_de_comando,
        default=None,
        help="data usada para calcular idades (padrão: hoje)",
    )
    return parser


def main(argv=None):
    argumentos = criar_parser().parse_args(argv)
    try:
        resultado = analisar_arquivo(
            argumentos.arquivo, argumentos.data_referencia
        )
    except FileNotFoundError:
        print(f"Erro: o arquivo '{argumentos.arquivo}' não foi encontrado.",
              file=sys.stderr)
        return 1
    except KeyError as erro:
        print(f"Erro: coluna obrigatória ausente no CSV: {erro.args[0]}",
              file=sys.stderr)
        return 1
    except UnicodeDecodeError:
        print("Erro: o arquivo não está codificado em UTF-8.",
              file=sys.stderr)
        return 1
    except (OSError, csv.Error) as erro:
        print(f"Erro ao ler '{argumentos.arquivo}': {erro}", file=sys.stderr)
        return 1
    else:
        relatorio = gerar_relatorio(resultado)
        print(relatorio)
        if salvar_relatorio(relatorio, argumentos.saida):
            print(f"\nRelatório salvo em '{argumentos.saida}'.")
        return 0
    finally:
        print("Análise encerrada.")


if __name__ == "__main__":
    sys.exit(main())
