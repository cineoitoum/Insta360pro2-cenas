import csv, os, shutil, sys, tempfile, threading
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import organizar_cenas as oc

OK = []
def check(cond, desc):
    print(("  PASS " if cond else "  FAIL ") + desc)
    OK.append(cond)

def w(p, conteudo, mtime=None):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(conteudo if isinstance(conteudo, bytes) else conteudo.encode())
    if mtime:
        os.utime(p, (mtime, mtime))

T1, T2, T3, T4 = "20261108_162608", "20261108_163140", "20261108_170500", "20261108_170800"

def montar(base):
    """6 cartões (1 por lente); o preview .flv fica na pasta VID_ do cartão 1."""
    o = os.path.join(base, "origem")
    for ts in (T1, T2, T3, T4):
        for n in range(1, 7):
            if ts == T4 and n == 3:
                continue                                      # T4 sem lente 3
            w(f"{o}/CARD{n}/VID_{ts}/origin_{n}_{ts}.mp4", f"L{n}-{ts}" * 500, 1_700_000_000)
        w(f"{o}/CARD1/VID_{ts}/origin_1_{ts}.flv", f"prev-{ts}" * 100)
        w(f"{o}/CARD1/VID_{ts}/pro.prj", "projeto")            # não é um dos 7: ignorado
        w(f"{o}/CARD2/VID_{ts}/.DS_Store", "lixo")
        w(f"{o}/CARD2/VID_{ts}/._origin_2_{ts}.mp4", "lixo")
    # cartão 3 descarregado duas vezes, com conteúdo diferente (colisão)
    w(f"{o}/BACKUP/CARD3/VID_{T1}/origin_3_{T1}.mp4", "versao-diferente")
    w(f"{o}/OUTRA/qualquer.mp4", "nao e da camera")
    return o

def arvore(p):
    return sorted((os.path.relpath(os.path.join(r, f), p), os.path.getsize(os.path.join(r, f)))
                  for r, _, fs in os.walk(p) for f in fs)

def ler_csv(p):
    with open(p, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f, delimiter=";"))

def novo(nome):
    b = os.path.join(base, nome)
    o = montar(b); d = os.path.join(b, "projeto"); os.makedirs(d)
    return o, d

base = tempfile.mkdtemp(prefix="cenas_teste_")

print("[1] Varredura")
o, d = novo("t1")
takes, ign = oc.varrer_takes(o, d)
check(list(takes) == [T1, T2, T3, T4], "4 takes, em ordem de hora")
check(all(len(takes[T2]["lentes"][n]) == 1 for n in range(1, 7)) and len(takes[T2]["preview"]) == 1,
      "T2 completo: 6 lentes + preview")
r4 = oc.resumo_take(takes[T4])
check(r4["faltam"] == [3] and not r4["completo"], f"T4 incompleto: {r4['avisos']}")
r1 = oc.resumo_take(takes[T1])
check(r1["duplos"] == [3], f"T1 com lente 3 repetida (cartão descarregado 2x): {r1['avisos']}")
check(ign == 4, "pro.prj (4x) ignorado; .DS_Store/._* nem contam; OUTRA/qualquer.mp4 fora")

print("\n[2] Sugestão de cenas (intervalo entre inícios de take)")
s = oc.sugerir_cenas(takes, {}, 15, [])
print("   15 min:", s)
check(s == {T1: "Cena 01", T2: "Cena 01", T3: "Cena 02", T4: "Cena 02"}, "16:26/16:31 | 17:05/17:08")
check(len(set(oc.sugerir_cenas(takes, {}, 60, []).values())) == 1, "60 min → tudo uma cena")
s2 = oc.sugerir_cenas(takes, {T1: "Cena 01 - Chegada"}, 15, ["Cena 02 - Sala"])
print("   com T1 já atribuído e 'Cena 02 - Sala' existente:", s2)
check(T1 not in s2 and s2[T2] == "Cena 03" and s2[T3] == "Cena 04",
      "não mexe no que já tem cena e não reutiliza números existentes")

print("\n[3] Mapa take→cena salvo e recarregado")
atrib = {T1: "Cena 01 - Chegada", T2: "Cena 01 - Chegada", T3: "Cena 02 - Cozinha"}  # T4 sem cena
notas = {T2: "melhor take"}
oc.salvar_mapa(d, takes, atrib, notas)
a2, n2 = oc.carregar_mapa(d)
check(a2 == atrib and n2 == notas, "mapa_cenas.csv ida e volta")
check(oc.limpar_nome_cena(' Cena 3: Sala/Cozinha ') == "Cena 3- Sala-Cozinha", "nome de cena sem / e :")

print("\n[4] Simulação")
antes = arvore(o)
plano = oc.planejar(takes, atrib, d)
print("   " + oc.relatorio(plano, takes, oc.checar_espaco(plano, o, d, False)).replace("\n", "\n   "))
check(plano["sem_cena"] == [T4], "T4 sem cena fica de fora")
check(plano["total_arquivos"] == 3 * 7 + 1, "22 arquivos (3 takes x 7 + lente 3 repetida)")
check(len(plano["colisoes"]) == 1 and plano["colisoes"][0]["alvo"].endswith(f"origin_3_{T1}.mp4"),
      "colisão da lente 3 avisada")
check(arvore(o) == antes and arvore(d) == [(oc.MAPA, os.path.getsize(os.path.join(d, oc.MAPA)))],
      "simulação não alterou nada (só o mapa existe no projeto)")

print("\n[5] Copiar")
r = oc.executar(plano, d, False, True, threading.Event(), lambda m: None)
pt1 = os.path.join(d, "Cena 01 - Chegada", f"VID_{T1}")
conteudo = sorted(os.listdir(pt1))
print("   Cena 01/VID_T1:", conteudo)
check(conteudo == sorted([f"origin_{n}_{T1}.mp4" for n in range(1, 7)] +
                         [f"origin_1_{T1}.flv", f"origin_3_{T1} (copia 1).mp4"]),
      "6 lentes + preview + colisão como (copia 1); pro.prj não copiado")
check(sorted(os.listdir(os.path.join(d, "Cena 01 - Chegada"))) == [f"VID_{T1}", f"VID_{T2}"] and
      os.listdir(os.path.join(d, "Cena 02 - Cozinha")) == [f"VID_{T3}"], "takes nas cenas certas")
check(os.path.getmtime(os.path.join(pt1, f"origin_2_{T1}.mp4")) == 1_700_000_000, "datas preservadas")
check(arvore(o) == antes, "origem intacta ao copiar")
lin = ler_csv(r["csv"])
check(len(lin) == 22 and [l["acao"] for l in lin].count("duplicado") == 1 and
      all("sha256 ok" in l["verificacao"] for l in lin), "CSV: 22 linhas, 1 duplicado, SHA-256 ok")
print("   CSV (3 primeiras):")
for l in lin[:3]:
    print(f"     {l['cena']} | VID_{l['take']} | {l['tipo']:8} | {l['arquivo']} | {l['acao']}")

print("\n[6] Projeto já organizado é reconhecido na próxima varredura")
ja, cenas = oc.ler_projeto(d)
check(ja == {T1: "Cena 01 - Chegada", T2: "Cena 01 - Chegada", T3: "Cena 02 - Cozinha"} and
      cenas == ["Cena 01 - Chegada", "Cena 02 - Cozinha"], "takes e cenas existentes detectados")
plano2 = oc.planejar(takes, atrib, d)
check(len(plano2["colisoes"]) == 21, "rodar de novo: todos os 21 destinos avisados como colisão")

print("\n[7] Mover")
o7, d7 = novo("t7")
tk7, _ = oc.varrer_takes(o7, d7)
at7 = {ts: "Cena A" for ts in tk7}
r7 = oc.executar(oc.planejar(tk7, at7, d7), d7, True, False, threading.Event(), lambda m: None)
sobra = arvore(o7)
print("   resumo:", {k: v for k, v in r7.items() if k != "csv"})
print("   sobrou na origem:", sorted({p.split(os.sep)[0] + "/…/" + os.path.basename(p) for p, _ in sobra}))
check(r7["movidos"] == 28 and r7["erros"] == 0, "28 arquivos movidos (4 takes)")
check(all(os.path.basename(p) == "pro.prj" or p.startswith("OUTRA") for p, _ in sobra),
      "na origem só sobrou pro.prj e o que não é da câmera")
check(not os.path.exists(f"{o7}/CARD2/VID_{T1}") and os.path.isdir(f"{o7}/CARD1/VID_{T1}"),
      "pasta VID_ vazia removida (mesmo com .DS_Store); a que tem pro.prj ficou")

print("\n[8] Mover com falha de verificação")
o8, d8 = novo("t8")
tk8, _ = oc.varrer_takes(o8, d8)
original = oc.copiar_arquivo
def corromper(src, tmp, c, p, h):
    res = original(src, tmp, c, p, h)
    if src.endswith(f"origin_5_{T2}.mp4"):
        open(tmp, "ab").write(b"X")
    return res
oc.copiar_arquivo = corromper
logs8 = []
r8 = oc.executar(oc.planejar(tk8, {T2: "Cena B"}, d8), d8, True, False, threading.Event(), logs8.append)
oc.copiar_arquivo = original
print("   ", [l for l in logs8 if l.startswith("ERRO")][0])
check(r8["erros"] == 1 and r8["movidos"] == 6, "1 erro, os outros 6 seguiram")
check(os.path.exists(f"{o8}/CARD5/VID_{T2}/origin_5_{T2}.mp4") and
      not os.path.exists(f"{d8}/Cena B/VID_{T2}/origin_5_{T2}.mp4"), "original mantido, cópia ruim descartada")

print("\n[9] Cancelamento no meio de um arquivo grande")
o9 = os.path.join(base, "t9", "origem"); d9 = os.path.join(base, "t9", "projeto"); os.makedirs(d9)
w(f"{o9}/CARD1/VID_{T1}/origin_1_{T1}.mp4", "x" * 100)
w(f"{o9}/CARD2/VID_{T1}/origin_2_{T1}.mp4", os.urandom(64 * 1024 * 1024))
w(f"{o9}/CARD3/VID_{T1}/origin_3_{T1}.mp4", "y" * 100)
oc.CHUNK = 1024 * 1024
tk9, _ = oc.varrer_takes(o9, d9)
cancel = threading.Event()
r9 = oc.executar(oc.planejar(tk9, {T1: "C"}, d9), d9, True, False, cancel, lambda m: None,
                 lambda f, t: cancel.set() if f > 20 * 1024 * 1024 else None)
fin = sorted(os.listdir(f"{d9}/C/VID_{T1}"))
print("   no projeto:", fin)
check(r9["cancelado"] and fin == [f"origin_1_{T1}.mp4"], "só o arquivo concluído ficou; parcial apagado")
check(os.path.exists(f"{o9}/CARD2/VID_{T1}/origin_2_{T1}.mp4") and
      os.path.exists(f"{o9}/CARD3/VID_{T1}/origin_3_{T1}.mp4"), "originais pendentes intactos")

print("\n[10] Validações")
check(oc.validar_pastas(o, os.path.join(o, "CARD1")) is not None, "projeto dentro da origem bloqueado")
oc.MARGEM_ESPACO = 10 ** 18
check(not oc.checar_espaco(plano, o, d, False)[0], "bloqueia quando falta espaço")

print(f"\n{sum(OK)}/{len(OK)} verificações passaram")
shutil.rmtree(base)
sys.exit(0 if all(OK) else 1)
