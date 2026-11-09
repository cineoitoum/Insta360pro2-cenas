#!/usr/bin/env python3
"""
organizar_cenas.py — organiza takes da Insta360 Pro 2 em pastas de cena.

Varre a ORIGEM (cartões / HD) recursivamente, reconhece os arquivos
    origin_N_AAAAMMDD_HHMMSS.mp4   (N = lente 1..6)
    origin_N_AAAAMMDD_HHMMSS.flv   (preview)
agrupa por take (timestamp) e mostra uma linha por take. Você atribui takes
a cenas (pasta existente ou nova) e o app copia/move os 7 arquivos de cada
take para  PROJETO/<Cena>/VID_<timestamp>/.

Nunca sobrescreve (colisão vira "nome (copia N).ext"), verifica cada arquivo,
gera log CSV e salva o mapa take→cena em PROJETO/mapa_cenas.csv.

Uso:  python3 organizar_cenas.py      (só biblioteca padrão, tkinter)
"""

import csv
import hashlib
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import uuid
from datetime import datetime

CHUNK = 8 * 1024 * 1024
MARGEM_ESPACO = 200 * 1024 * 1024
LENTES = (1, 2, 3, 4, 5, 6)
MAPA = "mapa_cenas.csv"

RE_ARQ = re.compile(r"^origin_([1-6])_(\d{8}_\d{6})\.(mp4|flv)$", re.IGNORECASE)
RE_PASTA = re.compile(r"^VID_(\d{8}_\d{6})")


class Cancelado(Exception):
    pass


# ============================================================================
# Utilitários
# ============================================================================

def ignorar(nome):
    return nome == ".DS_Store" or nome.startswith("._")


def fmt_bytes(n):
    for unid in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unid == "TB":
            return f"{n:.0f} {unid}" if unid == "B" else f"{n:.2f} {unid}"
        n /= 1024.0


def _dentro(filho, pai):
    try:
        return os.path.commonpath([filho, pai]) == pai
    except ValueError:
        return False


def validar_pastas(origem, destino):
    if not origem or not os.path.isdir(origem):
        return "Escolha uma pasta de ORIGEM válida."
    if not destino or not os.path.isdir(destino):
        return "Escolha uma pasta de PROJETO (destino) válida."
    o, d = os.path.realpath(origem), os.path.realpath(destino)
    if o == d:
        return "ORIGEM e PROJETO são a mesma pasta. Escolha uma pasta nova para o projeto."
    if _dentro(d, o):
        return "A pasta do PROJETO está dentro da ORIGEM. Escolha um destino fora dela."
    if _dentro(o, d):
        return "A ORIGEM está dentro da pasta do PROJETO. Escolha pastas separadas."
    return None


def limpar_nome_cena(nome):
    nome = re.sub(r'[/\\:*?"<>|]', "-", (nome or "")).strip().lstrip(".")
    return re.sub(r"\s+", " ", nome)


def ts_para_dt(ts):
    return datetime.strptime(ts, "%Y%m%d_%H%M%S")


def nome_livre(caminho):
    if not os.path.lexists(caminho):
        return caminho
    base, ext = os.path.splitext(caminho)
    n = 1
    while True:
        cand = f"{base} (copia {n}){ext}"
        if not os.path.lexists(cand):
            return cand
        n += 1


# ============================================================================
# Varredura
# ============================================================================

def novo_take(ts):
    return {"ts": ts, "dt": ts_para_dt(ts),
            "lentes": {n: [] for n in LENTES},   # n -> [(caminho, tamanho)]
            "preview": []}                       # [(caminho, tamanho)]


def varrer_takes(origem, destino=None, cancel=None, log=lambda m: None):
    """Retorna (takes: {ts: take}, ignorados: int)."""
    destino_real = os.path.realpath(destino) if destino else None
    takes, ignorados = {}, 0

    def erro_walk(e):
        log(f"AVISO: não foi possível ler {e.filename}: {e.strerror}")

    for raiz, dirs, arquivos in os.walk(origem, onerror=erro_walk):
        if cancel is not None and cancel.is_set():
            raise Cancelado()
        dirs[:] = sorted(d for d in dirs
                         if not os.path.islink(os.path.join(raiz, d))
                         and os.path.realpath(os.path.join(raiz, d)) != destino_real)
        # timestamp da pasta VID_ mais próxima (se houver)
        ts_pasta = None
        for parte in reversed(os.path.relpath(raiz, origem).split(os.sep)):
            m = RE_PASTA.match(parte)
            if m:
                ts_pasta = m.group(1)
                break
        for nome in sorted(arquivos):
            if ignorar(nome):
                continue
            caminho = os.path.join(raiz, nome)
            m = RE_ARQ.match(nome)
            if not m or os.path.islink(caminho):
                if ts_pasta:
                    ignorados += 1
                continue
            lente, ts, ext = int(m.group(1)), m.group(2), m.group(3).lower()
            try:
                ts_para_dt(ts)
                tam = os.path.getsize(caminho)
            except (ValueError, OSError) as e:
                log(f"AVISO: ignorado {caminho}: {e}")
                continue
            if ts_pasta and ts_pasta != ts:
                log(f"AVISO: {nome} está na pasta VID_{ts_pasta} — agrupado pelo nome do arquivo ({ts}).")
            t = takes.setdefault(ts, novo_take(ts))
            if ext == "flv":
                t["preview"].append((caminho, tam))
            else:
                t["lentes"][lente].append((caminho, tam))
    return dict(sorted(takes.items())), ignorados


def resumo_take(t):
    faltam = [n for n in LENTES if not t["lentes"][n]]
    duplos = [n for n in LENTES if len(t["lentes"][n]) > 1]
    tam = sum(s for n in LENTES for _p, s in t["lentes"][n]) + sum(s for _p, s in t["preview"])
    avisos = []
    if faltam:
        avisos.append("falta lente " + ",".join(map(str, faltam)))
    if not t["preview"]:
        avisos.append("sem preview")
    if duplos:
        avisos.append("repetida lente " + ",".join(map(str, duplos)))
    if len(t["preview"]) > 1:
        avisos.append("preview repetido")
    return {"faltam": faltam, "duplos": duplos, "tamanho": tam, "avisos": avisos,
            "completo": not faltam and bool(t["preview"])}


def ler_projeto(destino):
    """Takes já organizados no projeto: {ts: cena} e lista de cenas existentes."""
    ja, cenas = {}, []
    if not destino or not os.path.isdir(destino):
        return ja, cenas
    for cena in sorted(os.listdir(destino)):
        pc = os.path.join(destino, cena)
        if cena.startswith(".") or not os.path.isdir(pc):
            continue
        cenas.append(cena)
        try:
            for sub in os.listdir(pc):
                m = RE_PASTA.match(sub)
                if m and os.path.isdir(os.path.join(pc, sub)):
                    ja.setdefault(m.group(1), cena)
        except OSError:
            pass
    return ja, cenas


# ============================================================================
# Mapa take → cena (+ nota)
# ============================================================================

def carregar_mapa(destino):
    atrib, notas = {}, {}
    p = os.path.join(destino, MAPA)
    if not os.path.isfile(p):
        return atrib, notas
    with open(p, encoding="utf-8-sig", newline="") as f:
        for linha in csv.DictReader(f, delimiter=";"):
            ts = (linha.get("take") or "").strip()
            if not ts:
                continue
            if (linha.get("cena") or "").strip():
                atrib[ts] = linha["cena"].strip()
            if (linha.get("nota") or "").strip():
                notas[ts] = linha["nota"].strip()
    return atrib, notas


def salvar_mapa(destino, takes, atrib, notas):
    p = os.path.join(destino, MAPA)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["take", "data", "hora", "cena", "nota", "lentes", "preview", "avisos"])
        for ts in sorted(set(takes) | set(atrib) | set(notas)):
            t = takes.get(ts)
            dt = ts_para_dt(ts)
            if t:
                r = resumo_take(t)
                lentes = "".join(str(n) for n in LENTES if t["lentes"][n])
                prev = "sim" if t["preview"] else "não"
                avisos = "; ".join(r["avisos"])
            else:
                lentes = prev = avisos = ""
            w.writerow([ts, dt.strftime("%d/%m/%Y"), dt.strftime("%H:%M:%S"),
                        atrib.get(ts, ""), notas.get(ts, ""), lentes, prev, avisos])
    os.replace(tmp, p)
    return p


def sugerir_cenas(takes, atrib, intervalo_min, cenas_conhecidas):
    """Agrupa takes SEM cena em cenas novas, quebrando quando o intervalo entre
    o início de dois takes consecutivos passa de `intervalo_min`.
    Takes já atribuídos não mudam (e interrompem o grupo).
    Retorna {ts: cena_sugerida}."""
    usados = set(cenas_conhecidas) | set(atrib.values())
    n = 0

    def proximo_nome():
        nonlocal n
        while True:
            n += 1
            nome = f"Cena {n:02d}"
            if not any(u == nome or u.startswith(nome + " ") for u in usados):
                usados.add(nome)
                return nome

    sugest, atual, anterior = {}, None, None
    for ts in sorted(takes):
        dt = takes[ts]["dt"]
        if ts in atrib:
            atual = None
        else:
            if atual is None or (dt - anterior).total_seconds() > intervalo_min * 60:
                atual = proximo_nome()
            sugest[ts] = atual
        anterior = dt
    return sugest


# ============================================================================
# Plano e execução
# ============================================================================

def planejar(takes, atrib, destino):
    plano = {"itens": [], "total_bytes": 0, "maior_arquivo": 0, "total_arquivos": 0,
             "sem_cena": [], "incompletos": [], "colisoes": [], "cenas": {}}
    alvos = {}
    for ts in sorted(takes):
        t = takes[ts]
        cena = atrib.get(ts)
        if not cena:
            plano["sem_cena"].append(ts)
            continue
        r = resumo_take(t)
        if r["avisos"]:
            plano["incompletos"].append((ts, cena, "; ".join(r["avisos"])))
        c = plano["cenas"].setdefault(cena, {"takes": [], "arquivos": 0, "bytes": 0})
        c["takes"].append(ts)
        arquivos = [(f"lente {n}", p, s) for n in LENTES for p, s in t["lentes"][n]]
        arquivos += [("preview", p, s) for p, s in t["preview"]]
        for tipo, src, tam in arquivos:
            alvo = os.path.join(destino, cena, f"VID_{ts}", os.path.basename(src))
            plano["itens"].append({"cena": cena, "take": ts, "tipo": tipo,
                                   "src": src, "tam": tam, "alvo": alvo})
            alvos.setdefault(alvo, []).append(src)
            plano["total_bytes"] += tam
            plano["total_arquivos"] += 1
            plano["maior_arquivo"] = max(plano["maior_arquivo"], tam)
            c["arquivos"] += 1
            c["bytes"] += tam
    for alvo, srcs in alvos.items():
        existe = os.path.lexists(alvo)
        if len(srcs) > 1 or existe:
            plano["colisoes"].append({"alvo": os.path.relpath(alvo, destino),
                                      "origens": srcs, "ja_existe": existe})
    return plano


def checar_espaco(plano, origem, destino, mover):
    livre = shutil.disk_usage(destino).free
    mesmo_disco = os.stat(origem).st_dev == os.stat(destino).st_dev
    necessario = plano["maior_arquivo"] if (mover and mesmo_disco) else plano["total_bytes"]
    return (necessario + MARGEM_ESPACO <= livre), necessario, livre


def relatorio(plano, takes, espaco=None):
    L = [f"Cenas: {len(plano['cenas'])}   Takes: {sum(len(c['takes']) for c in plano['cenas'].values())}   "
         f"Arquivos: {plano['total_arquivos']}   Total: {fmt_bytes(plano['total_bytes'])}"]
    for cena, c in sorted(plano["cenas"].items()):
        L.append(f"  {cena}: {len(c['takes'])} take(s), {c['arquivos']} arquivo(s), {fmt_bytes(c['bytes'])}")
        for ts in c["takes"]:
            L.append(f"      VID_{ts}  ({takes[ts]['dt'].strftime('%d/%m %H:%M:%S')})")
    if plano["sem_cena"]:
        L.append(f"\nSem cena (NÃO serão processados): {len(plano['sem_cena'])} take(s): "
                 + ", ".join(plano["sem_cena"]))
    if plano["incompletos"]:
        L.append(f"\nATENÇÃO — takes com problema ({len(plano['incompletos'])}):")
        for ts, cena, av in plano["incompletos"]:
            L.append(f"  VID_{ts} [{cena}]: {av}")
    if plano["colisoes"]:
        L.append(f"\nATENÇÃO — {len(plano['colisoes'])} arquivo(s) com o MESMO NOME no destino "
                 "(serão salvos como \"(copia N)\"):")
        for c in plano["colisoes"]:
            extra = " + já existe no projeto" if c["ja_existe"] else ""
            L.append(f"  {c['alvo']}  ←  {'; '.join(c['origens'])}{extra}")
    if espaco:
        ok, nec, livre = espaco
        L.append(f"\nEspaço: necessário {fmt_bytes(nec)}, livre {fmt_bytes(livre)} → "
                 f"{'OK' if ok else 'INSUFICIENTE'}")
    return "\n".join(L)


def copiar_arquivo(src, dst_tmp, cancel, progresso, calcular_hash):
    """Equivalente a shutil.copy2 (dados + datas), em blocos para permitir cancelar."""
    h = hashlib.sha256() if calcular_hash else None
    with open(src, "rb") as fi, open(dst_tmp, "xb") as fo:
        while True:
            if cancel.is_set():
                raise Cancelado()
            bloco = fi.read(CHUNK)
            if not bloco:
                break
            fo.write(bloco)
            if h:
                h.update(bloco)
            progresso(len(bloco))
        fo.flush()
        os.fsync(fo.fileno())
    shutil.copystat(src, dst_tmp)
    return h.hexdigest() if h else None


def sha256_arquivo(caminho, cancel):
    h = hashlib.sha256()
    with open(caminho, "rb") as f:
        if sys.platform == "darwin":
            try:
                import fcntl
                fcntl.fcntl(f.fileno(), 48, 1)  # F_NOCACHE: lê do disco, não do cache
            except Exception:
                pass
        while True:
            if cancel.is_set():
                raise Cancelado()
            bloco = f.read(CHUNK)
            if not bloco:
                break
            h.update(bloco)
    return h.hexdigest()


def remover_se_vazia(pasta, log):
    """Remove a pasta se só tiver .DS_Store/._* (ou nada)."""
    try:
        conteudo = os.listdir(pasta)
    except OSError:
        return 0
    if any(not ignorar(n) or not os.path.isfile(os.path.join(pasta, n)) for n in conteudo):
        return 0
    try:
        for n in conteudo:
            os.remove(os.path.join(pasta, n))
        os.rmdir(pasta)
        log(f"Pasta vazia removida: {pasta}")
        return 1
    except OSError:
        return 0


CSV_CAMPOS = ["cena", "take", "tipo", "arquivo", "origem", "destino_final",
              "acao", "tamanho_bytes", "verificacao"]


def executar(plano, destino, mover, verificar_hash, cancel,
             log=lambda m: None, progresso=lambda feito, total: None):
    agora = datetime.now().strftime("%Y%m%d_%H%M%S")
    caminho_csv = nome_livre(os.path.join(destino, f"log_{agora}.csv"))
    r = {"copiados": 0, "movidos": 0, "duplicados": 0, "erros": 0,
         "pastas_removidas": 0, "cancelado": False, "csv": caminho_csv}
    total = plano["total_bytes"] or 1
    feito = [0]
    pastas_origem = set()

    def avancar(n):
        feito[0] += n
        progresso(feito[0], total)

    with open(caminho_csv, "w", newline="", encoding="utf-8-sig") as fcsv:
        w = csv.writer(fcsv, delimiter=";")
        w.writerow(CSV_CAMPOS)
        take_atual = None
        try:
            for it in plano["itens"]:
                if cancel.is_set():
                    raise Cancelado()
                if (it["cena"], it["take"]) != take_atual:
                    take_atual = (it["cena"], it["take"])
                    log(f"== {it['cena']} / VID_{it['take']}")
                src, tam, alvo = it["src"], it["tam"], it["alvo"]
                nome = os.path.basename(src)
                inicio, tmp = feito[0], None
                try:
                    os.makedirs(os.path.dirname(alvo), exist_ok=True)
                    tmp = os.path.join(os.path.dirname(alvo), f".org_{uuid.uuid4().hex}.parcial")
                    h_src = copiar_arquivo(src, tmp, cancel, avancar, verificar_hash)
                    tam_dst = os.path.getsize(tmp)
                    if tam_dst != tam:
                        raise IOError(f"tamanho difere (origem {tam}, cópia {tam_dst})")
                    verif = "tamanho ok"
                    if verificar_hash:
                        if sha256_arquivo(tmp, cancel) != h_src:
                            raise IOError("SHA-256 difere")
                        verif += f"; sha256 ok ({h_src[:12]}…)"
                    final = nome_livre(alvo)  # nunca sobrescrever
                    os.rename(tmp, final)
                    tmp = None
                    duplicado = final != alvo
                    if mover:
                        os.remove(src)
                        pastas_origem.add(os.path.dirname(src))
                    r["movidos" if mover else "copiados"] += 1
                    if duplicado:
                        acao = "duplicado"
                        r["duplicados"] += 1
                        log(f"DUPLICADO: {nome} → {os.path.basename(final)}  (de {src})")
                    else:
                        acao = "movido" if mover else "copiado"
                        log(f"{acao}: {it['tipo']}  {nome}")
                    w.writerow([it["cena"], it["take"], it["tipo"], nome, src, final,
                                acao, tam, verif])
                    fcsv.flush()
                except Cancelado:
                    raise
                except Exception as e:
                    r["erros"] += 1
                    msg = f"{type(e).__name__}: {e}"
                    log(f"ERRO: {src} — {msg} (original mantido)")
                    w.writerow([it["cena"], it["take"], it["tipo"], nome, src, "",
                                "erro", tam, msg])
                    fcsv.flush()
                    avancar(max(0, tam - (feito[0] - inicio)))
                finally:
                    if tmp and os.path.exists(tmp):
                        try:
                            os.remove(tmp)
                        except OSError:
                            pass
        except Cancelado:
            r["cancelado"] = True
            log("CANCELADO. O arquivo em andamento foi descartado; os concluídos ficaram no projeto.")

    if mover:
        for p in sorted(pastas_origem, reverse=True):
            r["pastas_removidas"] += remover_se_vazia(p, log)

    log(f"Fim. copiados={r['copiados']} movidos={r['movidos']} duplicados={r['duplicados']} "
        f"erros={r['erros']} pastas vazias removidas={r['pastas_removidas']}"
        f"{' (CANCELADO)' if r['cancelado'] else ''}")
    log(f"Log CSV: {caminho_csv}")
    return r


# ============================================================================
# Interface
# ============================================================================

def rodar_interface():
    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog, ttk
    from tkinter.scrolledtext import ScrolledText

    class DialogoCena(tk.Toplevel):
        """Escolher cena existente ou digitar uma nova."""
        def __init__(self, pai, cenas, n_takes):
            super().__init__(pai)
            self.title("Atribuir à cena")
            self.transient(pai)
            self.resultado = None
            ttk.Label(self, text=f"{n_takes} take(s) selecionado(s).\n"
                                 "Escolha uma cena existente ou digite um nome novo\n"
                                 "(ex.: \"Cena 03 - Cozinha\"):").pack(padx=12, pady=(12, 6), anchor="w")
            self.v = tk.StringVar()
            e = ttk.Entry(self, textvariable=self.v, width=45)
            e.pack(padx=12, fill="x")
            self.lb = tk.Listbox(self, height=min(12, max(4, len(cenas))))
            for c in cenas:
                self.lb.insert("end", c)
            self.lb.pack(padx=12, pady=6, fill="both", expand=True)
            self.lb.bind("<<ListboxSelect>>", lambda _e: self._pegar())
            self.lb.bind("<Double-Button-1>", lambda _e: (self._pegar(), self.ok()))
            b = ttk.Frame(self)
            b.pack(pady=(0, 12))
            ttk.Button(b, text="OK", command=self.ok).pack(side="left", padx=4)
            ttk.Button(b, text="Cancelar", command=self.destroy).pack(side="left", padx=4)
            self.bind("<Return>", lambda _e: self.ok())
            self.bind("<Escape>", lambda _e: self.destroy())
            e.focus_set()
            self.grab_set()

        def _pegar(self):
            s = self.lb.curselection()
            if s:
                self.v.set(self.lb.get(s[0]))

        def ok(self):
            nome = limpar_nome_cena(self.v.get())
            if not nome:
                messagebox.showerror("Cena", "Digite ou escolha um nome de cena.", parent=self)
                return
            self.resultado = nome
            self.destroy()

    class App:
        COLS = ("hora", "l1", "l2", "l3", "l4", "l5", "l6", "prev", "tam", "cena", "nota", "status")

        def __init__(self, root):
            self.root = root
            root.title("Organizar cenas — Insta360 Pro 2")
            root.geometry("1150x780")
            self.fila = queue.Queue()
            self.cancel = threading.Event()
            self.trabalhando = False
            self.takes, self.atrib, self.notas = {}, {}, {}
            self.ja_no_projeto, self.cenas_projeto = {}, []
            self.origem_varrida = self.destino_varrido = None

            self.v_origem, self.v_destino = tk.StringVar(), tk.StringVar()
            self.v_modo = tk.StringVar(value="copiar")
            self.v_hash = tk.BooleanVar(value=False)
            self.v_gap = tk.IntVar(value=15)
            self.v_status = tk.StringVar(value="Escolha a origem e a pasta do projeto e clique em Varrer.")

            f = ttk.Frame(root, padding=8)
            f.pack(fill="both", expand=True)
            f.columnconfigure(1, weight=1)

            ttk.Label(f, text="Origem (cartões/HD):").grid(row=0, column=0, sticky="w")
            ttk.Entry(f, textvariable=self.v_origem).grid(row=0, column=1, sticky="ew", padx=5)
            ttk.Button(f, text="Escolher…", command=lambda: self.escolher(self.v_origem, "ORIGEM")).grid(row=0, column=2)
            ttk.Label(f, text="Pasta do projeto:").grid(row=1, column=0, sticky="w")
            ttk.Entry(f, textvariable=self.v_destino).grid(row=1, column=1, sticky="ew", padx=5)
            ttk.Button(f, text="Escolher…", command=lambda: self.escolher(self.v_destino, "PROJETO")).grid(row=1, column=2)

            l1 = ttk.Frame(f)
            l1.grid(row=2, column=0, columnspan=3, sticky="w", pady=6)
            self.b_varrer = ttk.Button(l1, text="Varrer", command=self.varrer)
            self.b_varrer.pack(side="left")
            ttk.Label(l1, text="   Nova cena quando o intervalo entre takes passar de").pack(side="left")
            ttk.Spinbox(l1, from_=1, to=600, width=5, textvariable=self.v_gap).pack(side="left", padx=3)
            ttk.Label(l1, text="min").pack(side="left")
            self.b_sugerir = ttk.Button(l1, text="Sugerir cenas", command=self.sugerir)
            self.b_sugerir.pack(side="left", padx=6)

            # tabela
            tf = ttk.Frame(f)
            tf.grid(row=3, column=0, columnspan=3, sticky="nsew")
            f.rowconfigure(3, weight=3)
            tf.columnconfigure(0, weight=1)
            tf.rowconfigure(0, weight=1)
            self.tree = ttk.Treeview(tf, columns=self.COLS, show="tree headings", selectmode="extended")
            self.tree.heading("#0", text="Take")
            self.tree.column("#0", width=185, stretch=False)
            titulos = {"hora": "Data / hora", "prev": "Prev", "tam": "Tamanho", "cena": "Cena",
                       "nota": "Nota", "status": "Status"}
            larg = {"hora": 115, "prev": 42, "tam": 80, "cena": 190, "nota": 170, "status": 230}
            for c in self.COLS:
                self.tree.heading(c, text=titulos.get(c, c.upper()))
                w = larg.get(c, 34)
                self.tree.column(c, width=w, stretch=c in ("cena", "nota", "status"),
                                 anchor="center" if w < 50 else "w")
            sb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
            self.tree.configure(yscrollcommand=sb.set)
            self.tree.grid(row=0, column=0, sticky="nsew")
            sb.grid(row=0, column=1, sticky="ns")
            self.tree.tag_configure("problema", foreground="#c0392b")
            self.tree.tag_configure("semcena", foreground="#7f8c8d")
            self.tree.bind("<Double-Button-1>", lambda _e: self.atribuir())

            l2 = ttk.Frame(f)
            l2.grid(row=4, column=0, columnspan=3, sticky="w", pady=4)
            self.bts_tabela = []
            for txt, cmd in (("Atribuir à cena…", self.atribuir), ("Tirar da cena", self.tirar),
                             ("Renomear cena…", self.renomear), ("Nota…", self.nota),
                             ("Selecionar sem cena", self.selecionar_sem_cena),
                             ("Abrir preview", self.abrir_preview)):
                b = ttk.Button(l2, text=txt, command=cmd)
                b.pack(side="left", padx=(0, 4))
                self.bts_tabela.append(b)

            l3 = ttk.Frame(f)
            l3.grid(row=5, column=0, columnspan=3, sticky="w", pady=4)
            ttk.Radiobutton(l3, text="Copiar", value="copiar", variable=self.v_modo).pack(side="left")
            ttk.Radiobutton(l3, text="Mover", value="mover", variable=self.v_modo).pack(side="left", padx=8)
            ttk.Checkbutton(l3, text="Verificar checksum (SHA-256, lento)",
                            variable=self.v_hash).pack(side="left", padx=12)
            self.b_sim = ttk.Button(l3, text="Simular", command=self.simular)
            self.b_sim.pack(side="left", padx=(20, 4))
            self.b_exe = ttk.Button(l3, text="Executar", command=self.executar)
            self.b_exe.pack(side="left", padx=4)
            self.b_can = ttk.Button(l3, text="Cancelar", command=self.cancelar, state="disabled")
            self.b_can.pack(side="left", padx=4)

            self.barra = ttk.Progressbar(f, maximum=1000)
            self.barra.grid(row=6, column=0, columnspan=3, sticky="ew", pady=2)
            ttk.Label(f, textvariable=self.v_status).grid(row=7, column=0, columnspan=3, sticky="w")
            self.txt = ScrolledText(f, height=10, font=("Menlo", 11))
            self.txt.grid(row=8, column=0, columnspan=3, sticky="nsew", pady=(4, 0))
            f.rowconfigure(8, weight=1)

            root.protocol("WM_DELETE_WINDOW", self.fechar)
            root.after(100, self.processar_fila)

        # ---------------------------------------------------------- básico
        def escolher(self, var, titulo):
            p = filedialog.askdirectory(title=f"Pasta de {titulo}")
            if p:
                var.set(p)

        def log(self, msg):
            self.fila.put(("log", msg))

        def escrever(self, msg):
            self.txt.insert("end", msg + "\n")
            self.txt.see("end")

        def ocupado(self, sim):
            self.trabalhando = sim
            st = "disabled" if sim else "normal"
            for b in [self.b_varrer, self.b_sugerir, self.b_sim, self.b_exe] + self.bts_tabela:
                b.config(state=st)
            self.b_can.config(state="normal" if sim else "disabled")

        def pastas_ok(self):
            o, d = self.v_origem.get().strip(), self.v_destino.get().strip()
            erro = validar_pastas(o, d)
            if erro:
                messagebox.showerror("Pastas", erro)
                return None
            return o, d

        def precisa_varredura(self):
            if not self.takes:
                messagebox.showinfo("Varrer", "Clique em Varrer primeiro.")
                return True
            if (self.v_origem.get().strip(), self.v_destino.get().strip()) != \
                    (self.origem_varrida, self.destino_varrido):
                messagebox.showinfo("Varrer", "As pastas mudaram. Clique em Varrer de novo.")
                return True
            return False

        def selecionados(self):
            return list(self.tree.selection())

        def cenas_conhecidas(self):
            return sorted(set(self.cenas_projeto) | set(self.atrib.values()))

        def salvar(self):
            try:
                salvar_mapa(self.destino_varrido, self.takes, self.atrib, self.notas)
            except OSError as e:
                self.escrever(f"AVISO: não foi possível salvar o mapa: {e}")

        # ---------------------------------------------------------- tabela
        def preencher(self):
            sel = set(self.tree.selection())
            self.tree.delete(*self.tree.get_children())
            for ts, t in self.takes.items():
                r = resumo_take(t)
                cel = []
                for n in LENTES:
                    q = len(t["lentes"][n])
                    cel.append("✗" if q == 0 else ("✓" if q == 1 else f"×{q}"))
                q = len(t["preview"])
                prev = "✗" if q == 0 else ("✓" if q == 1 else f"×{q}")
                status = list(r["avisos"])
                if ts in self.ja_no_projeto:
                    status.insert(0, f"já no projeto ({self.ja_no_projeto[ts]})")
                cena = self.atrib.get(ts, "")
                tags = ("problema",) if r["avisos"] else (() if cena else ("semcena",))
                self.tree.insert("", "end", iid=ts, text=f"VID_{ts}", tags=tags,
                                 values=(t["dt"].strftime("%d/%m %H:%M:%S"), *cel, prev,
                                         fmt_bytes(r["tamanho"]), cena or "(sem cena)",
                                         self.notas.get(ts, ""), "; ".join(status) or "ok"))
            self.tree.selection_set([s for s in sel if self.tree.exists(s)])
            sem = sum(1 for ts in self.takes if ts not in self.atrib)
            self.v_status.set(f"{len(self.takes)} take(s), {len(set(self.atrib.get(ts) for ts in self.takes if ts in self.atrib))} "
                              f"cena(s), {sem} sem cena.")

        def atribuir(self):
            if self.trabalhando or self.precisa_varredura():
                return
            sel = self.selecionados()
            if not sel:
                messagebox.showinfo("Atribuir", "Selecione um ou mais takes (Shift+clique para intervalo).")
                return
            d = DialogoCena(self.root, self.cenas_conhecidas(), len(sel))
            self.root.wait_window(d)
            if d.resultado:
                for ts in sel:
                    self.atrib[ts] = d.resultado
                self.salvar()
                self.preencher()

        def tirar(self):
            if self.precisa_varredura():
                return
            for ts in self.selecionados():
                self.atrib.pop(ts, None)
            self.salvar()
            self.preencher()

        def renomear(self):
            if self.precisa_varredura():
                return
            sel = self.selecionados()
            cenas = {self.atrib[ts] for ts in sel if ts in self.atrib}
            if len(cenas) != 1:
                messagebox.showinfo("Renomear", "Selecione takes de UMA cena para renomeá-la.")
                return
            velho = cenas.pop()
            if velho in self.cenas_projeto:
                messagebox.showwarning("Renomear",
                                       f"\"{velho}\" já existe como pasta no projeto. Renomeie a pasta no "
                                       "Finder e clique em Varrer de novo.")
                return
            novo = limpar_nome_cena(simpledialog.askstring("Renomear cena", "Novo nome:",
                                                           initialvalue=velho, parent=self.root))
            if novo and novo != velho:
                for ts, c in list(self.atrib.items()):
                    if c == velho:
                        self.atrib[ts] = novo
                self.salvar()
                self.preencher()

        def nota(self):
            if self.precisa_varredura():
                return
            sel = self.selecionados()
            if not sel:
                return
            atual = self.notas.get(sel[0], "") if len(sel) == 1 else ""
            txt = simpledialog.askstring("Nota", f"Nota para {len(sel)} take(s) "
                                         "(ex.: \"bom\", \"ator errou\"):", initialvalue=atual,
                                         parent=self.root)
            if txt is None:
                return
            for ts in sel:
                if txt.strip():
                    self.notas[ts] = txt.strip().replace(";", ",")
                else:
                    self.notas.pop(ts, None)
            self.salvar()
            self.preencher()

        def selecionar_sem_cena(self):
            self.tree.selection_set([ts for ts in self.takes if ts not in self.atrib])

        def abrir_preview(self):
            sel = self.selecionados()
            if len(sel) != 1 or not self.takes.get(sel[0], {}).get("preview"):
                messagebox.showinfo("Preview", "Selecione UM take que tenha preview.")
                return
            caminho = self.takes[sel[0]]["preview"][0][0]
            try:
                if sys.platform == "darwin":
                    subprocess.Popen(["open", caminho])
                elif os.name == "nt":
                    os.startfile(caminho)
                else:
                    subprocess.Popen(["xdg-open", caminho])
            except Exception as e:
                messagebox.showerror("Preview", f"Não foi possível abrir: {e}\n"
                                     "O QuickTime não abre .flv — instale o VLC ou o IINA.")

        def sugerir(self):
            if self.precisa_varredura():
                return
            try:
                gap = int(self.v_gap.get())
            except (tk.TclError, ValueError):
                messagebox.showerror("Intervalo", "Intervalo inválido.")
                return
            sug = sugerir_cenas(self.takes, self.atrib, gap, self.cenas_projeto)
            if not sug:
                messagebox.showinfo("Sugerir", "Todos os takes já têm cena.")
                return
            grupos = {}
            for ts, c in sug.items():
                grupos.setdefault(c, []).append(ts)
            resumo = "\n".join(f"{c}: {len(v)} take(s)  {self.takes[v[0]]['dt']:%H:%M} → "
                               f"{self.takes[v[-1]]['dt']:%H:%M}" for c, v in grupos.items())
            if messagebox.askyesno("Sugerir cenas",
                                   f"Proposta para os {len(sug)} take(s) sem cena "
                                   f"(intervalo > {gap} min = nova cena):\n\n{resumo}\n\n"
                                   "Aplicar? Depois você pode renomear (ex.: \"Cena 01 - Chegada\") "
                                   "e ajustar take a take."):
                self.atrib.update(sug)
                self.salvar()
                self.preencher()

        # ---------------------------------------------------------- threads
        def processar_fila(self):
            try:
                while True:
                    tipo, *dados = self.fila.get_nowait()
                    if tipo == "log":
                        self.escrever(dados[0])
                    elif tipo == "prog":
                        feito, total = dados
                        self.barra["value"] = 1000 * feito / total
                        self.v_status.set(f"{fmt_bytes(feito)} de {fmt_bytes(total)}")
                    elif tipo == "varrido":
                        self.fim_varredura(*dados)
                    elif tipo == "fim":
                        self.ocupado(False)
                        r = dados[0]
                        self.v_status.set("Cancelado." if r["cancelado"] else "Concluído.")
                        if r["erros"]:
                            messagebox.showwarning("Concluído com erros",
                                                   f"{r['erros']} erro(s). Veja o log:\n{r['csv']}")
                        self.varrer()  # atualiza a tabela com o novo estado do disco
                    elif tipo == "falha":
                        self.ocupado(False)
                        self.v_status.set("Falhou.")
                        messagebox.showerror("Erro", dados[0])
                    elif tipo == "cancelado":
                        self.ocupado(False)
                        self.v_status.set("Cancelado.")
            except queue.Empty:
                pass
            self.root.after(100, self.processar_fila)

        def varrer(self):
            p = self.pastas_ok()
            if not p:
                return
            o, d = p
            self.cancel.clear()
            self.ocupado(True)
            self.v_status.set("Varrendo…")

            def trabalho():
                try:
                    takes, ign = varrer_takes(o, d, self.cancel, self.log)
                    ja, cenas = ler_projeto(d)
                    atrib, notas = carregar_mapa(d)
                    self.fila.put(("varrido", o, d, takes, ign, ja, cenas, atrib, notas))
                except Cancelado:
                    self.fila.put(("cancelado",))
                except Exception as e:
                    self.fila.put(("falha", f"{type(e).__name__}: {e}"))
            threading.Thread(target=trabalho, daemon=True).start()

        def fim_varredura(self, o, d, takes, ign, ja, cenas, atrib, notas):
            self.ocupado(False)
            self.origem_varrida, self.destino_varrido = o, d
            self.takes, self.ja_no_projeto, self.cenas_projeto = takes, ja, cenas
            # prioridade: mapa salvo > onde o take já está no projeto
            self.atrib = {ts: c for ts, c in ja.items() if ts in takes}
            self.atrib.update(atrib)
            self.notas = notas
            self.preencher()
            self.escrever(f"Varredura: {len(takes)} take(s) encontrados"
                          + (f"; {ign} outro(s) arquivo(s) nas pastas VID_ ignorado(s)" if ign else "")
                          + (f"; mapa carregado de {MAPA}" if atrib or notas else "") + ".")

        def montar_plano(self):
            if self.precisa_varredura():
                return None
            return planejar(self.takes, self.atrib, self.destino_varrido)

        def simular(self):
            plano = self.montar_plano()
            if not plano:
                return
            esp = checar_espaco(plano, self.origem_varrida, self.destino_varrido,
                                self.v_modo.get() == "mover")
            self.txt.delete("1.0", "end")
            self.escrever("SIMULAÇÃO (nada foi alterado)\n" + relatorio(plano, self.takes, esp))
            self.v_status.set("Simulação concluída. Nada foi alterado.")

        def executar(self):
            plano = self.montar_plano()
            if not plano:
                return
            o, d = self.origem_varrida, self.destino_varrido
            mover = self.v_modo.get() == "mover"
            if not plano["itens"]:
                messagebox.showinfo("Executar", "Nenhum take tem cena atribuída.")
                return
            ok, nec, livre = checar_espaco(plano, o, d, mover)
            if not ok:
                messagebox.showerror("Espaço insuficiente",
                                     f"Necessário: {fmt_bytes(nec)} (+ folga)\nLivre: {fmt_bytes(livre)}")
                return
            avisos = []
            if plano["sem_cena"]:
                avisos.append(f"• {len(plano['sem_cena'])} take(s) sem cena NÃO serão processados.")
            if plano["incompletos"]:
                avisos.append(f"• {len(plano['incompletos'])} take(s) com lente/preview faltando ou repetido.")
            if plano["colisoes"]:
                lista = "\n".join("   " + c["alvo"] for c in plano["colisoes"][:10])
                mais = len(plano["colisoes"]) - 10
                avisos.append(f"• {len(plano['colisoes'])} arquivo(s) com o MESMO NOME — viram \"(copia N)\":\n"
                              + lista + (f"\n   … e mais {mais}" if mais > 0 else ""))
            if avisos and not messagebox.askyesno("Atenção", "\n".join(avisos) + "\n\nContinuar?"):
                return
            if mover and not messagebox.askyesno(
                    "Confirmar MOVER",
                    f"MOVER {plano['total_arquivos']} arquivo(s) ({fmt_bytes(plano['total_bytes'])}) "
                    f"para\n{d}?\n\nCada original só é apagado depois de copiado e verificado.\n"
                    "Pastas de origem que ficarem vazias serão removidas.",
                    icon="warning", default="no"):
                return
            self.txt.delete("1.0", "end")
            self.escrever(relatorio(plano, self.takes, (ok, nec, livre)))
            self.escrever("-" * 70)
            self.barra["value"] = 0
            verificar = self.v_hash.get()
            self.cancel.clear()
            self.ocupado(True)

            def trabalho():
                try:
                    r = executar(plano, d, mover, verificar, self.cancel, self.log,
                                 lambda fe, to: self.fila.put(("prog", fe, to)))
                    self.fila.put(("fim", r))
                except Exception as e:
                    self.fila.put(("falha", f"{type(e).__name__}: {e}"))
            threading.Thread(target=trabalho, daemon=True).start()

        def cancelar(self):
            self.cancel.set()
            self.v_status.set("Cancelando…")

        def fechar(self):
            if self.trabalhando and not messagebox.askyesno(
                    "Sair", "Há uma operação em andamento. Cancelar e sair?"):
                return
            self.cancel.set()
            self.root.destroy()

    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    rodar_interface()
