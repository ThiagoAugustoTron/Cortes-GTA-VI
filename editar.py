"""Aplica com ffmpeg o plano de edicao: previa no inicio, legenda, formato vertical."""
import json
import subprocess
from pathlib import Path

_nvenc_ruim = False


def _probe(video: Path) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format",
                        "-of", "json", str(video)], capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


def duracao_video(video: Path) -> float:
    return float(_probe(video)["format"]["duration"])


def _ass_t(s: float) -> str:
    cs = round(s * 100)
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def escrever_ass(legendas, caminho: Path, largura: int, altura: int, vertical: bool) -> None:
    tam = round(altura * (0.036 if vertical else 0.06))
    margem_v = round(altura * (0.20 if vertical else 0.05))   # vertical: abaixo do video, acima da UI
    linhas = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {largura}", f"PlayResY: {altura}",
        "WrapStyle: 0", "", "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,"
        "Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,"
        "Alignment,MarginL,MarginR,MarginV,Encoding",
        f"Style: Default,Arial,{tam},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,"
        f"0,0,1,{max(3, tam // 12)},0,2,{round(largura * 0.06)},{round(largura * 0.06)},{margem_v},1",
        "", "[Events]", "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text"]
    for a, b, txt in legendas:
        txt = txt.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")
        linhas.append(f"Dialogue: 0,{_ass_t(a)},{_ass_t(b)},Default,,0,0,0,,{txt}")
    caminho.write_text("\n".join(linhas) + "\n", encoding="utf-8")


def _legendas_finais(legendas, previa):
    """Tempos de cada legenda no video final (previa no inicio desloca o clipe inteiro)."""
    if not previa:
        return list(legendas)
    pa, pb = previa
    desloc = pb - pa
    saida = []
    for a, b, t in legendas:
        if b > pa and a < pb:                       # fala que cai dentro da previa
            saida.append((max(a, pa) - pa, min(b, pb) - pa, t))
        saida.append((a + desloc, b + desloc, t))   # e de novo no clipe completo
    return sorted(saida)


def editar(origem: Path, saida: Path, plano: dict, cfg: dict) -> Path:
    """plano: {previa: (ini, fim)|None, legendas: [(ini, fim, texto)]} (tempos do clipe original)."""
    global _nvenc_ruim
    e = cfg["edicao"]
    info = _probe(origem)
    tem_audio = any(s["codec_type"] == "audio" for s in info["streams"])
    vs = next(s for s in info["streams"] if s["codec_type"] == "video")
    vertical = e.get("formato", "vertical") == "vertical"
    largura, altura = (1080, 1920) if vertical else (int(vs["width"]), int(vs["height"]))

    previa = plano.get("previa") if e.get("usar_previa", True) else None
    legendas = plano.get("legendas") if e.get("usar_legendas", True) else []
    saida.parent.mkdir(parents=True, exist_ok=True)

    # 1) trilha de video/audio: [previa] + clipe completo
    if previa:
        a, b = previa
        f = ("[0:v]split=2[v1][v2];[v1]trim=start={a}:end={b},setpts=PTS-STARTPTS[pv];"
             "[v2]setpts=PTS-STARTPTS[mv];").format(a=a, b=b)
        if tem_audio:
            f += ("[0:a]asplit=2[a1][a2];[a1]atrim=start={a}:end={b},asetpts=PTS-STARTPTS[pa];"
                  "[a2]asetpts=PTS-STARTPTS[ma];[pv][pa][mv][ma]concat=n=2:v=1:a=1[v0][a];").format(a=a, b=b)
        else:
            f += "[pv][mv]concat=n=2:v=1:a=0[v0];"
    else:
        f = "[0:v]null[v0];"
    # 2) formato vertical: fundo desfocado (calculado em imagem 10x menor, bem mais rapido)
    if vertical:
        f += ("[v0]split=2[x][y];[x]scale=108:192:force_original_aspect_ratio=increase,crop=108:192,"
              "boxblur=3:2,scale=1080:1920[bg];[y]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2[v1]")
    else:
        f += "[v0]null[v1]"
    # 3) legenda queimada (ASS no mesmo diretorio de saida; caminho relativo p/ o Windows)
    final = _legendas_finais(legendas or [], previa)
    ass = saida.with_suffix(".ass")
    if final:
        escrever_ass(final, ass, largura, altura, vertical)
        f += f";[v1]subtitles={ass.name}[v]"
    else:
        f += ";[v1]null[v]"

    base = ["ffmpeg", "-y", "-v", "error", "-i", str(origem.resolve()), "-filter_complex", f,
            "-map", "[v]"] + (["-map", "[a]" if previa else "0:a"] if tem_audio else [])
    fim = (["-c:a", "aac", "-b:a", "128k"] if tem_audio else []) + [
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(saida.resolve())]
    # maxrate 4M: ate 90 s ~ 45 MB, cabe no limite de 64 MB do envio ao TikTok
    gpu = ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "24",
           "-maxrate", "4M", "-bufsize", "8M"]
    cpu = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-maxrate", "4M", "-bufsize", "8M"]
    try:
        if e.get("encoder", "auto") != "cpu" and not _nvenc_ruim:
            try:
                subprocess.run(base + gpu + fim, check=True, cwd=saida.parent, capture_output=True)   # sem a placa NVIDIA, cai na CPU em silencio
                return saida
            except (subprocess.CalledProcessError, FileNotFoundError):
                _nvenc_ruim = True
        subprocess.run(base + cpu + fim, check=True, cwd=saida.parent)
    finally:
        ass.unlink(missing_ok=True)
    return saida
