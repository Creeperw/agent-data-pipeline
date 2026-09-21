# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH).resolve().parent

transformers_datas, transformers_binaries, transformers_hiddenimports = collect_all("transformers")

datas = [
    (str(ROOT / "agent" / "ui" / "static"), "agent/ui/static"),
    (str(ROOT / "agent" / "ui" / "README.md"), "agent/ui"),
    (str(ROOT / "agent" / ".env.example"), "agent"),
    (str(ROOT / "packaging" / "README.md"), "."),
    (str(ROOT / "agent" / "domains"), "agent/domains"),
    (str(ROOT / "agent" / "tools"), "agent/tools"),
] + transformers_datas

hiddenimports = [
    "agent.generator",
    "agent.fix_empty_tool_call_intents",
    "agent.convert_to_sft",
    "agent.convert_to_final",
    "agent.executor_generator",
    "agent.reviewer_generator",
    "agent.merge_final_sft",
    "agent.filter_reviewed_sft",
    "agent.deduplicate",
    "agent.data_stats",
    "agent.merge_tasks",
    "agent.rag_summary_data",
    "agent.multiturn_cli",
    "agent.inject_identity",
    "agent.build_executor_no_intent_sft",
    "agent.downsample_intent_data",
    "agent.downsample_executor_data",
    "agent.domains.health",
    "agent.domains.health_talent",
    "agent.domains.customer_service",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "fastapi",
    "openai",
] + transformers_hiddenimports

a = Analysis(
    [str(ROOT / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=transformers_binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tensorflow"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AgentDataPipeline",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)
worker = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AgentDataPipelineWorker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
coll = COLLECT(
    exe,
    worker,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="AgentDataPipeline",
)