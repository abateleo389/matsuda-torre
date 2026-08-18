from flask import Flask, render_template, request, redirect, url_for, jsonify
import sqlite3
from datetime import datetime, timedelta

app = Flask(__name__)

DB = "torre.db"

MOTIVOS_PADRAO = [
    "Falta de paletes/quebrados",
    "Manutenção",
    "Falta de empilhadeira",
    "Máquina de costura",
    "Datadora"
]


def conectar():
    banco = sqlite3.connect(DB)
    banco.row_factory = sqlite3.Row
    return banco


def criar_banco():
    banco = conectar()

    banco.execute("""
    CREATE TABLE IF NOT EXISTS configuracoes (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        meta_minima INTEGER NOT NULL DEFAULT 3500,
        meta_maxima INTEGER NOT NULL DEFAULT 4000,
        meta_esperada_sacos INTEGER NOT NULL DEFAULT 3200,
        ritmo_minimo INTEGER NOT NULL DEFAULT 298,
        ritmo_maximo INTEGER NOT NULL DEFAULT 340,
        sacos_por_palete INTEGER NOT NULL DEFAULT 32,
        inicio_dia TEXT NOT NULL DEFAULT '06:30',
        fim_dia TEXT NOT NULL DEFAULT '18:15',
        inicio_noite TEXT NOT NULL DEFAULT '22:00',
        fim_noite TEXT NOT NULL DEFAULT '06:20'
    );
    """)

    colunas_para_adicionar = [
        ("meta_esperada_sacos", "INTEGER NOT NULL DEFAULT 3200"),
        ("ritmo_minimo", "INTEGER NOT NULL DEFAULT 298"),
        ("ritmo_maximo", "INTEGER NOT NULL DEFAULT 340")
    ]

    for coluna, tipo in colunas_para_adicionar:
        try:
            banco.execute(
                f"ALTER TABLE configuracoes ADD COLUMN {coluna} {tipo}"
            )
        except sqlite3.OperationalError:
            pass

    banco.executescript("""
    CREATE TABLE IF NOT EXISTS motivos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nome TEXT NOT NULL UNIQUE,
        ativo INTEGER NOT NULL DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS paradas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        motivo_id INTEGER NOT NULL,
        inicio TEXT NOT NULL,
        fim TEXT,
        observacao TEXT,
        FOREIGN KEY(motivo_id) REFERENCES motivos(id)
    );

    CREATE TABLE IF NOT EXISTS producao (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        data_hora TEXT NOT NULL,
        sacos INTEGER NOT NULL DEFAULT 0,
        paletes INTEGER NOT NULL DEFAULT 0,
        total_acumulado INTEGER NOT NULL DEFAULT 0,
        producao_hora INTEGER NOT NULL DEFAULT 0
    );
    """)

    # ==========================================================
    # ADICIONA AS NOVAS COLUNAS CASO O BANCO JÁ EXISTIA
    # ==========================================================

    colunas_producao = [
        ("total_acumulado", "INTEGER NOT NULL DEFAULT 0"),
        ("producao_hora", "INTEGER NOT NULL DEFAULT 0")
    ]

    for coluna, tipo in colunas_producao:
        try:
            banco.execute(
                f"ALTER TABLE producao ADD COLUMN {coluna} {tipo}"
            )
        except sqlite3.OperationalError:
            pass

    banco.execute(
        "INSERT OR IGNORE INTO configuracoes(id) VALUES(1)"
    )

    banco.execute(
        "UPDATE OR IGNORE motivos "
        "SET nome = 'Datadora' "
        "WHERE nome = 'Datadoura'"
    )

    for motivo in MOTIVOS_PADRAO:
        banco.execute(
            "INSERT OR IGNORE INTO motivos(nome) VALUES(?)",
            (motivo,)
        )

    banco.commit()
    banco.close()


def pegar_configuracoes():

    banco = conectar()

    row = banco.execute(
        "SELECT * FROM configuracoes WHERE id = 1"
    ).fetchone()

    banco.close()

    cfg = dict(row) if row else {}

    defaults = {
        "meta_minima": 3500,
        "meta_maxima": 4000,
        "meta_esperada_sacos": 3200,
        "ritmo_minimo": 298,
        "ritmo_maximo": 340,
        "sacos_por_palete": 32,
        "inicio_dia": "06:30",
        "fim_dia": "18:15",
        "inicio_noite": "22:00",
        "fim_noite": "06:20"
    }

    for key, val in defaults.items():

        if key not in cfg or cfg[key] is None:
            cfg[key] = val

    spp = (
        cfg["sacos_por_palete"]
        if cfg["sacos_por_palete"] > 0
        else 32
    )

    cfg["meta_esperada_paletes"] = round(
        cfg["meta_esperada_sacos"] / spp
    )

    cfg["ritmo_minimo_paletes"] = round(
        cfg["ritmo_minimo"] / spp
    )

    cfg["ritmo_maximo_paletes"] = round(
        cfg["ritmo_maximo"] / spp
    )

    return cfg


def determinar_turno_atual(cfg):

    agora = datetime.now().time()

    fmt = "%H:%M"

    inicio_dia = datetime.strptime(
        cfg.get("inicio_dia", "06:30"),
        fmt
    ).time()

    fim_dia = datetime.strptime(
        cfg.get("fim_dia", "18:15"),
        fmt
    ).time()

    inicio_noite = datetime.strptime(
        cfg.get("inicio_noite", "22:00"),
        fmt
    ).time()

    fim_noite = datetime.strptime(
        cfg.get("fim_noite", "06:20"),
        fmt
    ).time()

    if inicio_dia <= agora <= fim_dia:

        return (
            "DIURNO",
            f"{cfg.get('inicio_dia')} às {cfg.get('fim_dia')}"
        )

    if inicio_noite <= fim_noite:

        em_noite = (
            inicio_noite <= agora <= fim_noite
        )

    else:

        em_noite = (
            agora >= inicio_noite
            or agora <= fim_noite
        )

    if em_noite:

        return (
            "NOTURNO",
            f"{cfg.get('inicio_noite')} às {cfg.get('fim_noite')}"
        )

    return (
        "FORA DE TURNO",
        "--:-- às --:--"
    )


def calcular_duracao(inicio, fim):

    if not inicio:
        return None

    try:

        inicio_dt = datetime.fromisoformat(inicio)

        fim_dt = (
            datetime.fromisoformat(fim)
            if fim
            else datetime.now()
        )

        segundos = int(
            (fim_dt - inicio_dt).total_seconds()
        )

        horas = segundos // 3600

        minutos = (
            segundos % 3600
        ) // 60

        seg = segundos % 60

        return (
            f"{horas:02d}:"
            f"{minutos:02d}:"
            f"{seg:02d}"
        )

    except Exception:

        return None


def obter_paradas_resumo_hoje(banco, hoje):

    paradas = banco.execute("""
        SELECT
            paradas.inicio,
            paradas.fim,
            motivos.nome as motivo
        FROM paradas
        JOIN motivos
            ON motivos.id = paradas.motivo_id
        WHERE date(paradas.inicio) = ?
    """, (hoje,)).fetchall()

    total_segundos = 0

    agora = datetime.now()

    motivos_tempo = {}

    for p in paradas:

        dt_ini = datetime.fromisoformat(
            p["inicio"]
        )

        dt_fim = (
            datetime.fromisoformat(p["fim"])
            if p["fim"]
            else agora
        )

        duracao = int(
            (dt_fim - dt_ini).total_seconds()
        )

        total_segundos += duracao

        m_nome = p["motivo"]

        motivos_tempo[m_nome] = (
            motivos_tempo.get(m_nome, 0)
            + duracao
        )

    horas = total_segundos // 3600

    minutos = (
        total_segundos % 3600
    ) // 60

    segs = total_segundos % 60

    total_str = (
        f"{horas:02d}:"
        f"{minutos:02d}:"
        f"{segs:02d}"
    )

    resumo_motivos = []

    for motivo_nome, segs_motivo in motivos_tempo.items():

        m_horas = segs_motivo // 3600

        m_mins = (
            segs_motivo % 3600
        ) // 60

        m_segs = segs_motivo % 60

        pct = (
            segs_motivo
            / total_segundos
            * 100
            if total_segundos > 0
            else 0
        )

        resumo_motivos.append({
            "motivo": motivo_nome,
            "tempo_str": (
                f"{m_horas:02d}:"
                f"{m_mins:02d}:"
                f"{m_segs:02d}"
            ),
            "porcentagem": f"{pct:.1f}%"
        })

    return (
        total_str,
        total_segundos,
        resumo_motivos
    )


# ======================================================================
# RELATÓRIOS
# ======================================================================

def gerar_dados_relatorio(data_inicio, data_fim):

    banco = conectar()

    configuracao = pegar_configuracoes()

    producao_dias = banco.execute("""
        SELECT
            date(data_hora) AS data,
            COALESCE(SUM(sacos), 0) AS sacos,
            COALESCE(
                SUM(paletes),
                0
            ) AS paletes
        FROM producao
        WHERE date(data_hora) BETWEEN ? AND ?
        GROUP BY date(data_hora)
        ORDER BY date(data_hora)
    """, (
        data_inicio,
        data_fim
    )).fetchall()

    paradas = banco.execute("""
        SELECT
            motivos.nome AS motivo,
            paradas.inicio,
            paradas.fim
        FROM paradas
        JOIN motivos
            ON motivos.id = paradas.motivo_id
        WHERE date(paradas.inicio)
              BETWEEN ? AND ?
        ORDER BY paradas.inicio
    """, (
        data_inicio,
        data_fim
    )).fetchall()

    banco.close()

    producao_dict = {}

    for item in producao_dias:

        producao_dict[item["data"]] = {
            "sacos": item["sacos"],
            "paletes": item["paletes"]
        }

    inicio = datetime.strptime(
        data_inicio,
        "%Y-%m-%d"
    ).date()

    fim = datetime.strptime(
        data_fim,
        "%Y-%m-%d"
    ).date()

    dias = []

    data_atual = inicio

    while data_atual <= fim:

        data_str = data_atual.strftime(
            "%Y-%m-%d"
        )

        dados = producao_dict.get(
            data_str,
            {
                "sacos": 0,
                "paletes": 0
            }
        )

        sacos = dados["sacos"]

        paletes = dados["paletes"]

        meta = configuracao.get(
            "meta_minima",
            3500
        )

        percentual = (
            (sacos / meta) * 100
            if meta > 0
            else 0
        )

        dias.append({
            "data": data_str,
            "data_formatada": data_atual.strftime(
                "%d/%m/%Y"
            ),
            "sacos": sacos,
            "paletes": paletes,
            "meta": meta,
            "percentual": round(
                percentual,
                1
            )
        })

        data_atual += timedelta(days=1)

    producao_total = sum(
        d["sacos"]
        for d in dias
    )

    total_paletes = sum(
        d["paletes"]
        for d in dias
    )

    dias_com_producao = [
        d for d in dias
        if d["sacos"] > 0
    ]

    media_diaria = (
        producao_total
        / len(dias_com_producao)
        if dias_com_producao
        else 0
    )

    melhor_dia = (
        max(
            dias_com_producao,
            key=lambda x: x["sacos"]
        )
        if dias_com_producao
        else None
    )

    pior_dia = (
        min(
            dias_com_producao,
            key=lambda x: x["sacos"]
        )
        if dias_com_producao
        else None
    )

    total_segundos_parado = 0

    paradas_por_motivo = {}

    agora = datetime.now()

    for parada in paradas:

        inicio_parada = datetime.fromisoformat(
            parada["inicio"]
        )

        fim_parada = (
            datetime.fromisoformat(
                parada["fim"]
            )
            if parada["fim"]
            else agora
        )

        segundos = int(
            (
                fim_parada
                - inicio_parada
            ).total_seconds()
        )

        if segundos < 0:
            segundos = 0

        total_segundos_parado += segundos

        motivo = parada["motivo"]

        if motivo not in paradas_por_motivo:

            paradas_por_motivo[motivo] = 0

        paradas_por_motivo[motivo] += segundos

    def formatar_tempo(segundos):

        horas = segundos // 3600

        minutos = (
            segundos % 3600
        ) // 60

        segundos_restantes = (
            segundos % 60
        )

        return (
            f"{horas:02d}:"
            f"{minutos:02d}:"
            f"{segundos_restantes:02d}"
        )

    total_paradas = len(paradas)

    resumo_motivos = []

    for motivo, segundos in sorted(
        paradas_por_motivo.items(),
        key=lambda x: x[1],
        reverse=True
    ):

        percentual = (
            segundos
            / total_segundos_parado
            * 100
            if total_segundos_parado > 0
            else 0
        )

        resumo_motivos.append({
            "motivo": motivo,
            "segundos": segundos,
            "tempo": formatar_tempo(segundos),
            "percentual": round(
                percentual,
                1
            )
        })

    dias_acima_meta = len([
        d for d in dias
        if d["sacos"] >= d["meta"]
    ])

    dias_abaixo_meta = len([
        d for d in dias
        if d["sacos"] > 0
        and d["sacos"] < d["meta"]
    ])

    return {
        "data_inicio": data_inicio,
        "data_fim": data_fim,
        "dias": dias,

        "producao_total": producao_total,
        "total_paletes": total_paletes,
        "media_diaria": round(
            media_diaria
        ),

        "melhor_dia": melhor_dia,
        "pior_dia": pior_dia,

        "total_segundos_parado":
            total_segundos_parado,

        "tempo_parado":
            formatar_tempo(
                total_segundos_parado
            ),

        "total_paradas":
            total_paradas,

        "resumo_motivos":
            resumo_motivos,

        "dias_acima_meta":
            dias_acima_meta,

        "dias_abaixo_meta":
            dias_abaixo_meta,

        "configuracao":
            configuracao
    }


# ======================================================================
# PAINEL PRINCIPAL
# ======================================================================

@app.route("/")
def inicio():

    configuracao = pegar_configuracoes()

    banco = conectar()

    hoje = datetime.now().strftime(
        "%Y-%m-%d"
    )

    nome_turno, horario_turno = (
        determinar_turno_atual(
            configuracao
        )
    )

    row_prod = banco.execute("""
        SELECT
            COALESCE(SUM(sacos), 0) AS sacos,
            COALESCE(SUM(paletes), 0) AS paletes
        FROM producao
        WHERE date(data_hora) = ?
    """, (hoje,)).fetchone()

    producao = (
        dict(row_prod)
        if row_prod
        else {
            "sacos": 0,
            "paletes": 0
        }
    )

    sacos_por_palete = configuracao.get(
        "sacos_por_palete",
        32
    )

    if (
        producao["paletes"] == 0
        and producao["sacos"] > 0
    ):

        producao["paletes"] = (
            producao["sacos"]
            // sacos_por_palete
        )

    meta = configuracao.get(
        "meta_minima",
        3500
    )

    sacos_prod = producao.get(
        "sacos",
        0
    )

    pct_num = (
        round(
            (sacos_prod / meta) * 100,
            1
        )
        if meta > 0
        else 0.0
    )

    pct_progresso = f"{pct_num:.1f}%"

    largura_barra_css = (
        f"{min(pct_num, 100.0):.1f}%"
    )

    tempo_parado_str, total_segs_parado, resumo_paradas = (
        obter_paradas_resumo_hoje(
            banco,
            hoje
        )
    )

    row_parada = banco.execute("""
        SELECT
            paradas.*,
            motivos.nome AS motivo
        FROM paradas
        JOIN motivos
            ON motivos.id = paradas.motivo_id
        WHERE paradas.fim IS NULL
        ORDER BY paradas.inicio DESC
        LIMIT 1
    """).fetchone()

    parada_atual = (
        dict(row_parada)
        if row_parada
        else None
    )

    paradas_banco = banco.execute("""
        SELECT
            paradas.*,
            motivos.nome AS motivo
        FROM paradas
        JOIN motivos
            ON motivos.id = paradas.motivo_id
        ORDER BY paradas.inicio DESC
        LIMIT 10
    """).fetchall()

    paradas = []

    for parada in paradas_banco:

        p_dict = dict(parada)

        p_dict["duracao"] = calcular_duracao(
            p_dict["inicio"],
            p_dict["fim"]
        )

        paradas.append(p_dict)

    motivos_banco = banco.execute(
        "SELECT * FROM motivos "
        "WHERE ativo = 1 "
        "ORDER BY nome"
    ).fetchall()

    motivos = [
        dict(m)
        for m in motivos_banco
    ]

    banco.close()

    return render_template(
        "index.html",
        configuracao=configuracao,
        producao=producao,
        parada_atual=parada_atual,
        paradas=paradas,
        motivos=motivos,
        tempo_parado=tempo_parado_str,
        resumo_paradas=resumo_paradas,
        nome_turno=nome_turno,
        horario_turno=horario_turno,
        pct_progresso=pct_progresso,
        largura_barra_css=largura_barra_css
    )


# ======================================================================
# PRODUÇÃO
# ======================================================================

@app.route("/producao")
def pagina_producao():

    banco = conectar()

    historico_prod = banco.execute("""
        SELECT *
        FROM producao
        ORDER BY data_hora DESC
        LIMIT 50
    """).fetchall()

    banco.close()

    return render_template(
        "producao.html",
        producao_lista=[
            dict(p)
            for p in historico_prod
        ]
    )


# ======================================================================
# REGISTRAR PRODUÇÃO
# ======================================================================

@app.post("/producao/registrar")
@app.post("/producao")
def registrar_producao():

    try:

        # ==========================================================
        # O OPERADOR DIGITA O TOTAL ACUMULADO
        # ==========================================================

        total_acumulado = int(
            request.form.get("sacos") or 0
        )

    except ValueError:

        return redirect(
            url_for("inicio")
        )

    if total_acumulado < 0:

        return redirect(
            url_for("inicio")
        )

    configuracao = pegar_configuracoes()

    sacos_por_palete = configuracao.get(
        "sacos_por_palete",
        32
    )

    banco = conectar()

    hoje = datetime.now().strftime(
        "%Y-%m-%d"
    )

    # ==========================================================
    # PEGA O ÚLTIMO REGISTRO DO DIA
    # ==========================================================

    ultimo = banco.execute("""
        SELECT *
        FROM producao
        WHERE date(data_hora) = ?
        ORDER BY data_hora DESC
        LIMIT 1
    """, (hoje,)).fetchone()

    # ==========================================================
    # CALCULA A PRODUÇÃO DA HORA
    # ==========================================================

    if ultimo:

        ultimo_total = (
            ultimo["total_acumulado"]
        )

        producao_hora = (
            total_acumulado
            - ultimo_total
        )

        # Impede valor menor que o registro anterior
        if producao_hora < 0:

            banco.close()

            return redirect(
                request.referrer
                or url_for("inicio")
            )

    else:

        # Primeiro registro do dia
        producao_hora = total_acumulado

    # ==========================================================
    # CALCULA PALETES DA PRODUÇÃO DA HORA
    # ==========================================================

    paletes = (
        producao_hora
        // sacos_por_palete
        if sacos_por_palete > 0
        else 0
    )

    # ==========================================================
    # SALVA
    # ==========================================================

    banco.execute("""
        INSERT INTO producao(
            data_hora,
            sacos,
            paletes,
            total_acumulado,
            producao_hora
        )
        VALUES (?, ?, ?, ?, ?)
    """, (
        datetime.now().isoformat(
            timespec="seconds"
        ),
        producao_hora,
        paletes,
        total_acumulado,
        producao_hora
    ))

    banco.commit()

    banco.close()

    referrer = request.referrer or url_for(
        "inicio"
    )

    return redirect(referrer)


# ======================================================================
# PARADAS
# ======================================================================

@app.route("/paradas")
def pagina_paradas():

    banco = conectar()

    paradas_banco = banco.execute("""
        SELECT
            paradas.*,
            motivos.nome AS motivo
        FROM paradas
        JOIN motivos
            ON motivos.id = paradas.motivo_id
        ORDER BY paradas.inicio DESC
    """).fetchall()

    paradas = []

    for parada in paradas_banco:

        p_dict = dict(parada)

        p_dict["duracao"] = calcular_duracao(
            p_dict["inicio"],
            p_dict["fim"]
        )

        paradas.append(p_dict)

    motivos_banco = banco.execute(
        "SELECT * FROM motivos "
        "WHERE ativo = 1 "
        "ORDER BY nome"
    ).fetchall()

    motivos = [
        dict(m)
        for m in motivos_banco
    ]

    banco.close()

    return render_template(
        "paradas.html",
        paradas=paradas,
        motivos=motivos
    )


@app.post("/parada/iniciar")
def iniciar_parada():

    motivo_id = request.form.get(
        "motivo_id"
    )

    if not motivo_id:

        return redirect(
            url_for("inicio")
        )

    try:

        motivo_id = int(motivo_id)

    except ValueError:

        return redirect(
            url_for("inicio")
        )

    banco = conectar()

    parada_aberta = banco.execute(
        "SELECT id FROM paradas "
        "WHERE fim IS NULL "
        "LIMIT 1"
    ).fetchone()

    if not parada_aberta:

        banco.execute("""
            INSERT INTO paradas(
                motivo_id,
                inicio
            )
            VALUES (?, ?)
        """, (
            motivo_id,
            datetime.now().isoformat(
                timespec="seconds"
            )
        ))

        banco.commit()

    banco.close()

    referrer = request.referrer or url_for(
        "inicio"
    )

    return redirect(referrer)


@app.post("/parada/encerrar")
def encerrar_parada():

    banco = conectar()

    parada = banco.execute("""
        SELECT id
        FROM paradas
        WHERE fim IS NULL
        ORDER BY inicio DESC
        LIMIT 1
    """).fetchone()

    if parada:

        banco.execute("""
            UPDATE paradas
            SET fim = ?
            WHERE id = ?
        """, (
            datetime.now().isoformat(
                timespec="seconds"
            ),
            parada["id"]
        ))

        banco.commit()

    banco.close()

    referrer = request.referrer or url_for(
        "inicio"
    )

    return redirect(referrer)


# ======================================================================
# HISTÓRICO
# ======================================================================

@app.route("/historico")
def pagina_historico():

    banco = conectar()

    historico = banco.execute("""
        SELECT
            'Produção' as tipo,
            data_hora as data_inicio,
            sacos || ' sacos' as detalhe
        FROM producao

        UNION ALL

        SELECT
            'Parada' as tipo,
            paradas.inicio as data_inicio,
            motivos.nome as detalhe
        FROM paradas
        JOIN motivos
            ON motivos.id = paradas.motivo_id

        ORDER BY data_inicio DESC
        LIMIT 100
    """).fetchall()

    banco.close()

    return render_template(
        "historico.html",
        historico=[
            dict(h)
            for h in historico
        ]
    )


# ======================================================================
# RELATÓRIOS
# ======================================================================

@app.route("/relatorios")
def pagina_relatorios():

    hoje = datetime.now().date()

    periodo = request.args.get(
        "periodo",
        "mes"
    )

    data_inicio_param = request.args.get(
        "inicio"
    )

    data_fim_param = request.args.get(
        "fim"
    )

    if data_inicio_param and data_fim_param:

        data_inicio = data_inicio_param
        data_fim = data_fim_param

    elif periodo == "hoje":

        data_inicio = hoje.strftime(
            "%Y-%m-%d"
        )

        data_fim = data_inicio

    elif periodo == "ontem":

        ontem = hoje - timedelta(days=1)

        data_inicio = ontem.strftime(
            "%Y-%m-%d"
        )

        data_fim = data_inicio

    elif periodo == "7dias":

        inicio = hoje - timedelta(days=6)

        data_inicio = inicio.strftime(
            "%Y-%m-%d"
        )

        data_fim = hoje.strftime(
            "%Y-%m-%d"
        )

    else:

        inicio_mes = hoje.replace(
            day=1
        )

        data_inicio = inicio_mes.strftime(
            "%Y-%m-%d"
        )

        data_fim = hoje.strftime(
            "%Y-%m-%d"
        )

        periodo = "mes"

    relatorio = gerar_dados_relatorio(
        data_inicio,
        data_fim
    )

    return render_template(
        "relatorios.html",
        relatorio=relatorio,
        periodo=periodo
    )


# ======================================================================
# METAS
# ======================================================================

@app.route("/metas")
def pagina_metas():

    configuracao = pegar_configuracoes()

    return render_template(
        "metas.html",
        configuracao=configuracao
    )


# ======================================================================
# ZERAR PRODUÇÃO
# ======================================================================

@app.post("/zerar/producao-hoje")
def zerar_producao_hoje():

    hoje = datetime.now().strftime(
        "%Y-%m-%d"
    )

    banco = conectar()

    banco.execute(
        "DELETE FROM producao "
        "WHERE date(data_hora) = ?",
        (hoje,)
    )

    banco.commit()

    banco.close()

    return redirect(
        url_for("configuracoes")
    )


# ======================================================================
# ZERAR PARADAS
# ======================================================================

@app.post("/zerar/paradas-hoje")
def zerar_paradas_hoje():

    hoje = datetime.now().strftime(
        "%Y-%m-%d"
    )

    banco = conectar()

    banco.execute(
        "DELETE FROM paradas "
        "WHERE date(inicio) = ?",
        (hoje,)
    )

    banco.commit()

    banco.close()

    return redirect(
        url_for("configuracoes")
    )


# ======================================================================
# ZERAR TUDO
# ======================================================================

@app.post("/zerar/tudo-hoje")
def zerar_tudo_hoje():

    hoje = datetime.now().strftime(
        "%Y-%m-%d"
    )

    banco = conectar()

    banco.execute(
        "DELETE FROM producao "
        "WHERE date(data_hora) = ?",
        (hoje,)
    )

    banco.execute(
        "DELETE FROM paradas "
        "WHERE date(inicio) = ?",
        (hoje,)
    )

    banco.commit()

    banco.close()

    return redirect(
        url_for("configuracoes")
    )


# ======================================================================
# CONFIGURAÇÕES
# ======================================================================

@app.route(
    "/configuracoes",
    methods=["GET", "POST"]
)
def configuracoes():

    banco = conectar()

    if request.method == "POST":

        try:

            meta_minima = int(
                request.form.get(
                    "meta_minima"
                ) or 3500
            )

            meta_maxima = int(
                request.form.get(
                    "meta_maxima"
                ) or 4000
            )

            meta_esperada_sacos = int(
                request.form.get(
                    "meta_esperada_sacos"
                ) or 3200
            )

            ritmo_minimo = int(
                request.form.get(
                    "ritmo_minimo"
                ) or 298
            )

            ritmo_maximo = int(
                request.form.get(
                    "ritmo_maximo"
                ) or 340
            )

            sacos_por_palete = int(
                request.form.get(
                    "sacos_por_palete"
                ) or 32
            )

        except ValueError:

            (
                meta_minima,
                meta_maxima,
                meta_esperada_sacos,
                ritmo_minimo,
                ritmo_maximo,
                sacos_por_palete
            ) = (
                3500,
                4000,
                3200,
                298,
                340,
                32
            )

        inicio_dia = request.form.get(
            "inicio_dia",
            "06:30"
        )

        fim_dia = request.form.get(
            "fim_dia",
            "18:15"
        )

        inicio_noite = request.form.get(
            "inicio_noite",
            "22:00"
        )

        fim_noite = request.form.get(
            "fim_noite",
            "06:20"
        )

        banco.execute("""
            UPDATE configuracoes
            SET
                meta_minima = ?,
                meta_maxima = ?,
                meta_esperada_sacos = ?,
                ritmo_minimo = ?,
                ritmo_maximo = ?,
                sacos_por_palete = ?,
                inicio_dia = ?,
                fim_dia = ?,
                inicio_noite = ?,
                fim_noite = ?
            WHERE id = 1
        """, (
            meta_minima,
            meta_maxima,
            meta_esperada_sacos,
            ritmo_minimo,
            ritmo_maximo,
            sacos_por_palete,
            inicio_dia,
            fim_dia,
            inicio_noite,
            fim_noite
        ))

        banco.commit()

        banco.close()

        return redirect(
            url_for("configuracoes")
        )

    configuracao = pegar_configuracoes()

    motivos_banco = banco.execute("""
        SELECT *
        FROM motivos
        ORDER BY ativo DESC, nome
    """).fetchall()

    motivos = [
        dict(m)
        for m in motivos_banco
    ]

    banco.close()

    return render_template(
        "configuracoes.html",
        configuracao=configuracao,
        motivos=motivos
    )


# ======================================================================
# MOTIVOS
# ======================================================================

@app.post("/motivo/adicionar")
def adicionar_motivo():

    nome = request.form.get(
        "nome",
        ""
    ).strip()

    if nome:

        banco = conectar()

        banco.execute(
            "INSERT OR IGNORE INTO motivos(nome) VALUES(?)",
            (nome,)
        )

        banco.commit()

        banco.close()

    return redirect(
        url_for("configuracoes")
    )


@app.post("/motivo/<int:motivo_id>/alternar")
def alternar_motivo(motivo_id):

    banco = conectar()

    banco.execute("""
        UPDATE motivos
        SET ativo = CASE
            WHEN ativo = 1 THEN 0
            ELSE 1
        END
        WHERE id = ?
    """, (motivo_id,))

    banco.commit()

    banco.close()

    return redirect(
        url_for("configuracoes")
    )


# ======================================================================
# INICIALIZAÇÃO
# ======================================================================

if __name__ == "__main__":

    criar_banco()

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=True
    )