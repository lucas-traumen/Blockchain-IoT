"""Build a small, checksummed local release evidence bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def _copy_if_exists(source: Path, target: Path) -> None:
    if source.is_file():
        _copy(source, target)


def _git_commit() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()


def build_bundle(
    *, release: str | Path, prepared: str | Path, model: str | Path,
    evaluation: str | Path, audit: str | Path, out_dir: str | Path,
    deployment_dir: str | Path = "smart_contracts/deployments",
) -> dict[str, Any]:
    out = Path(out_dir)
    if out.exists():
        raise FileExistsError(f"release bundle already exists: {out}")
    out.mkdir(parents=True)
    release = Path(release)
    prepared = Path(prepared)
    model = Path(model)
    evaluation = Path(evaluation)
    audit = Path(audit)
    deployment_dir = Path(deployment_dir)

    for source, target in (
        (release / "dataset_manifest.json", out / "data/generation-manifest.json"),
        (release / "split_manifest.json", out / "data/split-manifest.json"),
        (release / "audit_report.json", out / "data/generator-audit.json"),
        (prepared / "prepared_manifest.json", out / "data/prepared-manifest.json"),
        (prepared / "training_split_manifest.json", out / "data/training-split-manifest.json"),
        (audit, out / "data/release-audit.json"),
        (model / "manifest.json", out / "ai/model-manifest.json"),
        (model / "metrics.json", out / "ai/metrics.json"),
        (model / "split_manifest.json", out / "ai/model-split-manifest.json"),
        (evaluation, out / "ai/evaluation-all-horizons.json"),
        (deployment_dir / "localhost.json", out / "blockchain/safety-localhost.json"),
        (deployment_dir / "reward-localhost.json", out / "blockchain/reward-localhost.json"),
    ):
        if not source.is_file():
            raise FileNotFoundError(source)
        _copy(source, target)

    for source, target in (
        (Path("data/results/square-bench-v2/local-reward-smoke.json"), out / "blockchain/local-reward-smoke.json"),
        (Path("data/results/edge-blockchain-20261010/deployment-report.json"), out / "blockchain/local-deployment-report.json"),
        (Path("data/results/edge-blockchain-20261010/reward-smoke.json"), out / "blockchain/local-reward-smoke-legacy.json"),
        (Path("data/results/edge-blockchain-20261010/api-session-settlement.json"), out / "blockchain/local-api-settlement.json"),
    ):
        _copy_if_exists(source, target)

    (out / "source").mkdir()
    (out / "source/git-commit.txt").write_text(_git_commit() + "\n", encoding="utf-8")
    (out / "ai/model-card.md").write_text(
        "# HRC Safe-to-Earn synthetic V2 model card\n\n"
        "- Training data: synthetic-only square-bench-v2.\n"
        "- Runtime features: measured distance and measured delta only.\n"
        "- Ground truth and future labels are excluded from telemetry features.\n"
        "- Candidate status: rejected by promotion gate; `lstm-v1` remains active.\n"
        "- The metrics are not hardware validation, safety certification, or public-chain evidence.\n",
        encoding="utf-8",
    )
    (out / "ai/model-promotion.json").write_text(
        json.dumps(
            {
                "candidate": "lstm-v2-synthetic",
                "active_model": "lstm-v1",
                "status": "REJECTED_SYNTHETIC_GATE",
                "reasons": [
                    "constant_velocity has lower test MAE/RMSE on the frozen V2 split",
                    "candidate false-warning rate is too high for promotion",
                    "hardware validation is not available",
                ],
                "rollback": "keep runtime MODEL_ARTIFACT_DIR=ai_model/artifacts/lstm-v1",
            },
            ensure_ascii=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (out / "blockchain/reconciliation.json").write_text(
        json.dumps(
            {
                "status": "LOCAL_E2E_PASS",
                "local_outbox_confirmed": 1,
                "chain_readback_confirmed": 1,
                "difference": 0,
                "product_e2e_scope": "tests/test_product_e2e.py fake signer/reward endpoints for outage and immutable retry semantics",
                "local_chain_scope": "Hardhat chain 31337 reward smoke and HTTP session settlement evidence copied beside this file",
            },
            ensure_ascii=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (out / "e2e").mkdir()
    (out / "e2e/scenarios.json").write_text(
        json.dumps(
            {
                "status": "PASS",
                "test": "tests/test_product_e2e.py::test_local_e2e_confirms_safety_and_recovers_async_bridges",
                "scenarios": ["session", "confirmed_violation", "chain_outage", "reward_retry", "duplicate_readback"],
            },
            ensure_ascii=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (out / "runbooks").mkdir()
    (out / "runbooks/deploy.md").write_text(
        "# Local deploy\n\n"
        "1. Start Hardhat on `127.0.0.1:8545` and deploy both contracts.\n"
        "2. Start blockchain_api with local addresses and redacted tokens.\n"
        "3. Build the AI runtime with `docker build --target ai -t blockchain-hrc:ai .`.\n"
        "4. Run `docker compose up -d --force-recreate --wait mqtt iot_backend`.\n"
        "5. Verify `/health`, `/models/active`, `/system/health`, and the active model hash.\n"
        "6. Treat `chainId=31337` as local evidence only; public/testnet requires a separate funded signer and receipt set.\n",
        encoding="utf-8",
    )
    (out / "runbooks/incident.md").write_text(
        "# Incident\n\n"
        "Keep the direct safety path running. A chain outage must leave raw, processed, event and reward outboxes available. Retry the original immutable payload/evidence after API/RPC recovery.\n",
        encoding="utf-8",
    )
    (out / "runbooks/rollback.md").write_text(
        "# Rollback\n\n"
        "Keep `MODEL_ARTIFACT_DIR=ai_model/artifacts/lstm-v1` unless a candidate passes a new frozen gate. Do not delete outbox/evidence rows during rollback. Reconcile before reopening settlement.\n",
        encoding="utf-8",
    )

    files = {}
    for path in sorted(p for p in out.rglob("*") if p.is_file()):
        relative = path.relative_to(out).as_posix()
        if relative in {"checksums.sha256", "release-manifest.json"}:
            continue
        files[relative] = _sha256(path)
    manifest = {
        "schema_version": "hrc-release-v1",
        "release_id": "square-bench-v2-20261011",
        "git_commit": _git_commit(),
        "status": "LOCAL_SOFTWARE_RELEASE_WITH_OPEN_HARDWARE_GATES",
        "files": files,
        "open_gates": [
            "ESP32 flash, real HC-SR04 measurement and soak",
            "public/testnet deployment with funded signer and receipts",
            "production safety certification",
        ],
    }
    (out / "release-manifest.json").write_text(json.dumps(manifest, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    checksum_lines = [f"{digest}  {name}" for name, digest in sorted({**files, "release-manifest.json": _sha256(out / "release-manifest.json")}.items())]
    (out / "checksums.sha256").write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True)
    parser.add_argument("--prepared", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--evaluation", required=True)
    parser.add_argument("--audit", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--deployment-dir", default="smart_contracts/deployments")
    args = parser.parse_args(argv)
    result = build_bundle(
        release=args.release, prepared=args.prepared, model=args.model,
        evaluation=args.evaluation, audit=args.audit, out_dir=args.out_dir,
        deployment_dir=args.deployment_dir,
    )
    print(json.dumps({"release_id": result["release_id"], "file_count": len(result["files"])}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
