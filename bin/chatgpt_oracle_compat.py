from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Sequence

SUPPORTED_VERSION = "0.17.1"
RECOVERABLE_VERSIONS = ("0.16.1", "0.17.0", SUPPORTED_VERSION)
CREATE_NO_WINDOW = 0x08000000
PATCHES_0161 = {
    "dist/src/cli/browserTabs.js": {
        "patch": "browserTabs.patch",
        "pristine": "05256692ffa9b35415346963adde5ff42aeacd78ce46dd6f484496678f5d0281",
        "patched": "9329e259f030ecb4a935fb9e368bf55074bf0afe7ed5e5a0c6206a5f2bbacee4",
        "legacy_patched": ["1a6d3b9d7044d84300f630fe669b16d9cfec3925c427cfb4c3d1291205406dab"],
        "legacy_patch": "browserTabs.pre-readiness.patch",
    },
    "dist/src/browser/chromeLifecycle.js": {
        "patch": "chromeLifecycle.patch",
        "pristine": "9eaffd8264051266581548ea9dbee1152bd94b7a6032ed0441b1ba3c11c5b5e9",
        "patched": "d852372c9c16c9a130a280001e62312542092b0c38397907897217f8af0c559d",
    },
    "dist/src/browser/recoverConversation.js": {
        "patch": "recoverConversation.patch",
        "pristine": "8c7d841bc078af20c8922ec435f62e00df7a40605583fbd89334696b3ddb386b",
        "patched": "168d665fa7c6cc0ef5094a990e94e7a3ae57f2d3bebcc5c2625cb6cff0cb89b1",
        "legacy_patched": ["650ffe9bdbbaf799510e8cacaa8ba8407322bbbb175e790a3cf7777fa14772fe"],
    },
    "dist/src/browser/profileCopy.js": {
        "patch": "profileCopy.patch",
        "pristine": "06c692861f8a4c1a8769f957b9c582426a13bf4972262c47c1f24a87b239064f",
        "patched": "71459a25b7c46f57bae6f23a5498301f6f6a1d39addf0c1cd4eee1d99b03372c",
    },
    "dist/src/cli/browserConfig.js": {
        "patch": "browserConfig.patch",
        "pristine": "2e4bbf102eed4276e105671244c5bfcdf6870ea72f304d8bb7d2ede10b8872c6",
        "patched": "52de31fff0ee71a9f9ed96400565104ff19226274335433732dfc4a932ff2e93",
    },
    "dist/src/browser/index.js": {
        "patch": "browserIndex.patch",
        "pristine": "ea7461262078363517ad16fe57ac8766f06f037782954987520375dcbfd1c7af",
        "patched": "5f7bc607dae4667ad860d2aa125c138c053190e33f206237c24f5c6aab4bf14c",
        "legacy_patched": [
            "9168df2b3e8c4d1c962d05b198ceab1a9df9e50c7573453673212905e2bc5eba",
        ],
    },
    "dist/src/browser/actions/assistantResponse.js": {
        "patch": "assistantResponse.patch",
        "pristine": "0bbc106f79c6abf253690c83794a2dab1b432378f57e16542d15cfcd5365e16d",
        "patched": "18661304c7fb545bc327876d38045818cbd23257488137836d43661be8742af4",
    },
    "dist/src/browser/actions/promptComposer.js": {
        "patch": "promptComposer.patch",
        "pristine": "db090a5fb6d13c4c88a68b5e474a53a19c3857295a64c3ba4a0eef1868d06000",
        "patched": "02874d0f2fcd0f45c2c50385893a210e2be5822e1831fa81b99944728ed1cb79",
        "legacy_patched": [
            "a4bd36940deca4fd4ca63536df863fff330e438c82d47871834d56cdc064f2b5",
            "3eb83d2b1fc0d9e097a26eca282dc36c93dac1e8485410f8c3617c826538e409",
            # Pre-downgrade build that hard-failed a run when the app mention
            # overlay or pill could not be observed.
            "446ee00d52111c6ba41eea9b60752374603bd4ae3e52985e129e9f5625dadb28",
        ],
    },
    "dist/src/browser/actions/modelSelection.js": {
        "patch": "modelSelection.patch",
        "pristine": "62351158216c0f9f81652f072413487d2db12cd20a1cf7c21575a3f3a2074573",
        "patched": "7e19a5bfd10668929d24961259c4ddedfdd8c26bc85b3ac4672c29f1f40f74fc",
    },
}

PATCHES_0170 = {
    "dist/src/browser/chromeLifecycle.js": {
        "patch": "chromeLifecycle.patch",
        "pristine": "55e9858f54fb625dbe349b64837be511694ec018074db66e1cca161f5d47182d",
        "patched": "d2d4842f11aff03ffe6f19840443ccf4cd02167334eb0cf9f67a5f78ef4e780c",
    },
    "dist/src/browser/recoverConversation.js": {
        "patch": "recoverConversation.patch",
        "pristine": "d7e39d21acf07e6d227e761944519e11cd8d93930629cc87555d7de75a42d1ca",
        "patched": "cc2a036f6e2409ae7edceee1f381a5062cd6cc5cd1618af465a1b384081ed69e",
    },
    "dist/src/browser/profileCopy.js": {
        "patch": "profileCopy.patch",
        "pristine": "06c692861f8a4c1a8769f957b9c582426a13bf4972262c47c1f24a87b239064f",
        "patched": "71459a25b7c46f57bae6f23a5498301f6f6a1d39addf0c1cd4eee1d99b03372c",
    },
    "dist/src/cli/browserConfig.js": {
        "patch": "browserConfig.patch",
        "pristine": "989f14399c8aa51913752306135e11d97e4f1c55b2baf984907f1b54959cc340",
        "patched": "bd18d11e4770fa5335c889b7856622f2da4199351ec65bc17a5ec1f472e2506f",
    },
    "dist/src/browser/index.js": {
        "patch": "browserIndex.patch",
        "pristine": "335f29c8864399cf2795333e4da8b87bc1b3591c30862eb9e82ea12cd3b37d11",
        "patched": "9a78695ba89a6e7eb6761dd06b9be74d500ac65b585158d75f8fd3c7a6eb8895",
    },
    "dist/src/browser/actions/assistantResponse.js": {
        "patch": "assistantResponse.patch",
        "pristine": "0bbc106f79c6abf253690c83794a2dab1b432378f57e16542d15cfcd5365e16d",
        "patched": "18661304c7fb545bc327876d38045818cbd23257488137836d43661be8742af4",
    },
    "dist/src/browser/actions/promptComposer.js": {
        "patch": "promptComposer.patch",
        "pristine": "db090a5fb6d13c4c88a68b5e474a53a19c3857295a64c3ba4a0eef1868d06000",
        "patched": "a3882c7881a7e787a33092350c494d950a6f67c38e6801cd1eaff20ac317532f",
        "legacy_patched": [
            "02874d0f2fcd0f45c2c50385893a210e2be5822e1831fa81b99944728ed1cb79",
            "99e4307ccdda8256e352d09b149f795ba0766584cd3fa838ea1adb22fd5b63ba",
            "71769d77b50d2c66bf281a6d70a965eaa0d43bfd23aa7c0c6645d774f95604fa",
            "7523e315eb6c6f29e5567a994084a39b73adf0adc1aecb013831885a3474e9b8",
            "f34821a5c4ac51d55bf2da0e0b8c2a8a3b3cafd3b9b6b6010726f0b032a5ece8",
            "8bba8fd9a663c4c404ccf479a0193672624c0e42afb3a3e04edf832a4d9820f6",
            "4da4b078cfba5c239b4cae55a58f4c831a48edb72e66286b63f86d6c9aa86594",
        ],
    },
    "dist/src/browser/actions/thinkingTime.js": {
        "patch": "thinkingTime.patch",
        "pristine": "9d0c8ae34d72c6ab5ca4176ba2ac2b8431fbb93d6d4e73c0cc02f5d2eb8863b7",
        "patched": "7d475ed81ccee29a5b4107ed166584bcd3b0266bfd25e02ca7743bf24301e7f0",
    },
}

PATCHES_0171 = {
    "dist/src/browser/chromeLifecycle.js": {
        "patch": "chromeLifecycle.patch",
        "pristine": "312b45c44d4cd69a3a057e7bd1584b58182b4b37bc88f6ce6c7d11e216267c81",
        "patched": "61440e467d51031efb7bfc319aef05de7c9061585e5eec148d0e353938eb2093",
    },
    "dist/src/browser/recoverConversation.js": {
        "patch": "recoverConversation.patch",
        "pristine": "d7e39d21acf07e6d227e761944519e11cd8d93930629cc87555d7de75a42d1ca",
        "patched": "cc2a036f6e2409ae7edceee1f381a5062cd6cc5cd1618af465a1b384081ed69e",
    },
    "dist/src/browser/profileCopy.js": {
        "patch": "profileCopy.patch",
        "pristine": "06c692861f8a4c1a8769f957b9c582426a13bf4972262c47c1f24a87b239064f",
        "patched": "71459a25b7c46f57bae6f23a5498301f6f6a1d39addf0c1cd4eee1d99b03372c",
    },
    "dist/src/cli/browserConfig.js": {
        "patch": "browserConfig.patch",
        "pristine": "989f14399c8aa51913752306135e11d97e4f1c55b2baf984907f1b54959cc340",
        "patched": "bd18d11e4770fa5335c889b7856622f2da4199351ec65bc17a5ec1f472e2506f",
    },
    "dist/src/browser/index.js": {
        "patch": "browserIndex.patch",
        "pristine": "335f29c8864399cf2795333e4da8b87bc1b3591c30862eb9e82ea12cd3b37d11",
        "patched": "9a78695ba89a6e7eb6761dd06b9be74d500ac65b585158d75f8fd3c7a6eb8895",
    },
    "dist/src/browser/actions/assistantResponse.js": {
        "patch": "assistantResponse.patch",
        "pristine": "0bbc106f79c6abf253690c83794a2dab1b432378f57e16542d15cfcd5365e16d",
        "patched": "18661304c7fb545bc327876d38045818cbd23257488137836d43661be8742af4",
    },
    "dist/src/browser/actions/promptComposer.js": {
        "patch": "promptComposer.patch",
        "pristine": "db090a5fb6d13c4c88a68b5e474a53a19c3857295a64c3ba4a0eef1868d06000",
        "patched": "87911b46026d3dd08a643b90aab5dc7009704956a3b5fed493cc600abcb7739a",
        "legacy_patched": [
            # Both deployed Input.insertText levels fail to generate the key
            # event required by the current ChatGPT mention picker.
            "a3882c7881a7e787a33092350c494d950a6f67c38e6801cd1eaff20ac317532f",
            "bb85c6f09f23c4e0c9093bd472c83b17b1ef7325bcd89a3348429610eeefbd74",
        ],
        "legacy_patches": {
            "a3882c7881a7e787a33092350c494d950a6f67c38e6801cd1eaff20ac317532f": (
                "promptComposer.pre-split-trigger.patch"
            ),
            "bb85c6f09f23c4e0c9093bd472c83b17b1ef7325bcd89a3348429610eeefbd74": (
                "promptComposer.pre-key-event-trigger.patch"
            ),
        },
    },
    "dist/src/browser/actions/thinkingTime.js": {
        "patch": "thinkingTime.strict.patch",
        "pristine": "508f1fbc175b82e6bfd4c978da6199306800615f432e28d7721c155c402795ca",
        "patched": "3f969712b184588d1f34ef4f55b439c86256d112bb0fa1688bb473b61fd3dcc3",
        "legacy_patched": [
            # Fork levels: canonical LF hashes of the two shipped patch results.
            # Deployed Windows copies carried the raw CRLF hashes
            # 21027b691a... (extra-high fail-closed) and 300e910c1f... (Pro
            # heavy upgrade); canonical hashing makes both byte flavors bind
            # to these single hashes instead of accepting ambiguous ones.
            "4106ed89a032d06fadcf1c1600e238e26243c02d1c3ef4261ea70169396d464e",
            "1464b79c1d0bb8913963ab12c55fbc843fe760f367be2b68b1c36d62e43ff5e4",
            # Parent-project legacy levels from the audited 9542abee donor.
            "536571fccc3f8137bfbf0ea96dfd827f1eabdaf92f93fe7cff92af242ef01d53",
            "fe6db3c1d48ccf7eff212dab7e69a2b3c7439f44b5cc823d474aa4fbd0925151",
            "ce0fa250ba4b28aeff9e3e80267b3f55bd08f7d25c9890a0eb09debcae447b8b",
            "686e80ee7480686622eab7bc8863eccdf3ad57e64f662bfcbfbc4852802c7aaa",
            "4e73e1c1d9c04e7bea7811a5e32bf17c559a2e1171581dc4cc33f48163ef28e7",
            "374f0fabd62ea82ecf359c3050995da7a3de2d791905d04742f91ebe098d910a",
            "864f8365ecbd0aef9b631f7ae61c80b3e43424dc37c34cdfd5c6e5aa06b0c1b3",
            "d8fbe1394314efaa38343539ad7be519212fd5301f74e4aa92336f6925e3b5fd",
            "9ac1cab3200fb848ca2f88c07f98b19d94c7d4ad5a9b2e578c1c5a9dee4df15f",
            "2baba20f9162eea8b4659ff42d85c26064d037bb18dd90f2022cf4764ddd710d",
            "0cb7bf4774e5507fb97682cf4e350fea03998c2a44548065bf8e9eb57fe16707",
            "b55897a9d90627b226e39e77339819e446927ffc66f78181f5c2851cbcfe5f97",
        ],
        "legacy_patch": "thinkingTime.strict.pre-power.patch",
        "legacy_patches": {
            # The fork shipped two former levels: `thinkingTime.patch`
            # (extra-high fail-closed, raw CRLF 21027b69...) and that patch
            # plus `thinkingTime.pro-heavy-upgrade.patch` (raw CRLF
            # 300e910c...).  Each entry lists the forward-application order
            # used to build the level; migration reverses them.
            "4106ed89a032d06fadcf1c1600e238e26243c02d1c3ef4261ea70169396d464e": [
                "thinkingTime.extra-high-fail-closed.patch",
            ],
            "1464b79c1d0bb8913963ab12c55fbc843fe760f367be2b68b1c36d62e43ff5e4": [
                "thinkingTime.extra-high-fail-closed.patch",
                "thinkingTime.pro-heavy-upgrade.patch",
            ],
            "fe6db3c1d48ccf7eff212dab7e69a2b3c7439f44b5cc823d474aa4fbd0925151":
                "thinkingTime.strict.broken-power.patch",
            "ce0fa250ba4b28aeff9e3e80267b3f55bd08f7d25c9890a0eb09debcae447b8b":
                "thinkingTime.strict.double-escaped-power.patch",
            "686e80ee7480686622eab7bc8863eccdf3ad57e64f662bfcbfbc4852802c7aaa":
                "thinkingTime.strict.single-escaped-power.patch",
            "4e73e1c1d9c04e7bea7811a5e32bf17c559a2e1171581dc4cc33f48163ef28e7":
                "thinkingTime.strict.regex-power.patch",
            "374f0fabd62ea82ecf359c3050995da7a3de2d791905d04742f91ebe098d910a":
                "thinkingTime.strict.compact-power.patch",
            "864f8365ecbd0aef9b631f7ae61c80b3e43424dc37c34cdfd5c6e5aa06b0c1b3":
                "thinkingTime.strict.hidden-slider.patch",
            "d8fbe1394314efaa38343539ad7be519212fd5301f74e4aa92336f6925e3b5fd":
                "thinkingTime.strict.pro-proof-model-bound.patch",
            "9ac1cab3200fb848ca2f88c07f98b19d94c7d4ad5a9b2e578c1c5a9dee4df15f":
                "thinkingTime.strict.null-model-menu-closed.patch",
            "2baba20f9162eea8b4659ff42d85c26064d037bb18dd90f2022cf4764ddd710d":
                "thinkingTime.strict.pre-outer-model-proof.patch",
            "0cb7bf4774e5507fb97682cf4e350fea03998c2a44548065bf8e9eb57fe16707":
                "thinkingTime.strict.pre-visible-advanced-proof.patch",
            "b55897a9d90627b226e39e77339819e446927ffc66f78181f5c2851cbcfe5f97":
                "thinkingTime.strict.pre-advanced-view-sibling.patch",
        },
    },
}

VERSION_PATCHES = {"0.16.1": PATCHES_0161, "0.17.0": PATCHES_0170, "0.17.1": PATCHES_0171}
# Active-version alias retained for existing callers and tests.
PATCHES = VERSION_PATCHES[SUPPORTED_VERSION]


class OracleCompatError(RuntimeError):
    def __init__(self, code: str, message: str, evidence: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence or {}


def sha256_file(path: Path) -> str:
    # Fresh npx installs keep LF dist bytes; older Windows deployments of the
    # patched package can carry CRLF bytes for the same logical file.  Hash the
    # canonical LF form so one contract hash binds both byte flavors instead of
    # accepting two ambiguous hashes for the same patched result.
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def package_version(package_root: Path) -> str:
    try:
        value = json.loads((package_root / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OracleCompatError("ORACLE_PACKAGE_INVALID", "Oracle package.json is unreadable", {"root": str(package_root)}) from exc
    return str(value.get("version") or "").strip()


def _candidate_roots() -> list[Path]:
    override = str(os.environ.get("ORACLE_PACKAGE_ROOT") or "").strip()
    if override:
        return [Path(override).expanduser().resolve()]
    local = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    roots = list((local / "npm-cache" / "_npx").glob("*/node_modules/@steipete/oracle"))
    return sorted((path.resolve() for path in roots if path.is_dir()), key=lambda path: path.stat().st_mtime, reverse=True)


def resolve_package_root(version: str = SUPPORTED_VERSION) -> Path:
    candidates = _candidate_roots()
    matching = [path for path in candidates if package_version(path) == version]
    if not matching:
        raise OracleCompatError(
            "ORACLE_PACKAGE_NOT_FOUND",
            "The tested Oracle package is not installed in the npx cache",
            {"version": version, "candidates": [str(path) for path in candidates[:8]]},
        )
    return matching[0]


def resolve_package_roots(version: str = SUPPORTED_VERSION) -> list[Path]:
    candidates = _candidate_roots()
    matching = [path for path in candidates if package_version(path) == version]
    if not matching:
        raise OracleCompatError(
            "ORACLE_PACKAGE_NOT_FOUND",
            "The tested Oracle package is not installed in the npx cache",
            {"version": version, "candidates": [str(path) for path in candidates[:8]]},
        )
    return matching


def patch_root(version: str = SUPPORTED_VERSION) -> Path:
    return Path(__file__).resolve().parent / "oracle-compat" / version


def _git_kwargs() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    return {"creationflags": CREATE_NO_WINDOW, "startupinfo": startup}


def _apply_patch(package_root: Path, patch_path: Path, *, reverse: bool = False) -> None:
    isolated_env = os.environ.copy()
    # `git apply` also works outside a repository. Prevent a package root used
    # inside a test/worktree from being silently rebound to that parent repo.
    isolated_env["GIT_CEILING_DIRECTORIES"] = str(package_root.parent)
    patch_bytes = patch_path.read_bytes().replace(b"\r\n", b"\n")
    check = subprocess.run(
        ["git", "-c", "core.autocrlf=false", "apply", "--ignore-space-change", *( ["-R"] if reverse else [] ), "--check", "-"],
        cwd=str(package_root),
        input=patch_bytes,
        capture_output=True,
        check=False,
        env=isolated_env,
        **_git_kwargs(),
    )
    if check.returncode != 0:
        raise OracleCompatError(
            "ORACLE_PATCH_CHECK_FAILED",
            "Oracle compatibility patch no longer matches the installed package",
            {"patch": str(patch_path), "stderr": (check.stderr or b"").decode("utf-8", errors="replace").strip()[-1200:]},
        )
    applied = subprocess.run(
        ["git", "-c", "core.autocrlf=false", "apply", "--ignore-space-change", *( ["-R"] if reverse else [] ), "-"],
        cwd=str(package_root),
        input=patch_bytes,
        capture_output=True,
        check=False,
        env=isolated_env,
        **_git_kwargs(),
    )
    if applied.returncode != 0:
        raise OracleCompatError(
            "ORACLE_PATCH_APPLY_FAILED",
            "Oracle compatibility patch could not be applied",
            {"patch": str(patch_path), "stderr": (applied.stderr or b"").decode("utf-8", errors="replace").strip()[-1200:]},
        )


def _migrate_known_legacy_patch(
    package_root: Path,
    target: Path,
    relative: str,
    patch_paths: Sequence[Path],
    pristine_hash: str,
) -> None:
    """Restore one known former patch level to its verified pristine bytes.

    ``patch_paths`` lists the forward-application order of the legacy patches
    that produced the installed level; migration reverses them in reverse
    order and verifies the exact canonical pristine hash of the restored
    bytes.  Unknown levels are never guessed at and fail closed.
    """
    with tempfile.TemporaryDirectory(prefix="oracle-compat-migrate-") as temporary:
        staged_root = Path(temporary)
        staged_target = staged_root / Path(relative)
        staged_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, staged_target)
        for patch_path in reversed(patch_paths):
            _apply_patch(staged_root, patch_path, reverse=True)
        if sha256_file(staged_target) != pristine_hash:
            raise OracleCompatError(
                "ORACLE_LEGACY_PATCH_RESTORE_INVALID",
                "Known legacy Oracle patch did not restore the exact pristine bytes",
                {"path": str(target), "patch": [str(path) for path in patch_paths]},
            )
        shutil.copy2(staged_target, target)


def inspect_oracle_compatibility(
    resolved_version: str,
    *,
    package_root: Path | None = None,
) -> dict[str, Any]:
    """Inspect exact package hashes without patching or creating backups."""
    version = resolved_version.strip().removeprefix("oracle ").strip()
    contracts = VERSION_PATCHES.get(version)
    if contracts is None:
        raise OracleCompatError(
            "ORACLE_VERSION_UNVALIDATED",
            "Oracle compatibility is validated only for tested versions",
            {"resolved": resolved_version, "supported": list(RECOVERABLE_VERSIONS)},
        )
    roots = (
        resolve_package_roots(version)
        if package_root is None
        else [package_root.expanduser().resolve(strict=True)]
    )
    files: list[dict[str, str]] = []
    for root in roots:
        if package_version(root) != version:
            raise OracleCompatError(
                "ORACLE_VERSION_MISMATCH",
                "Oracle package version does not match the resolved CLI version",
            )
        for relative, contract in contracts.items():
            target = root / Path(relative)
            current = sha256_file(target)
            if current == contract["patched"]:
                status = "patched"
            elif current == contract["pristine"]:
                status = "patch_required"
            elif current in contract.get("legacy_patched", []):
                status = "legacy_patch_required"
            else:
                status = "drift"
            files.append({
                "path": str(target),
                "status": status,
                "actual_sha256": current,
                "expected_patched_sha256": str(contract["patched"]),
            })
    return {
        "ok": True,
        "ready": all(item["status"] == "patched" for item in files),
        "version": version,
        "package_root": str(roots[0]),
        "package_roots": [str(root) for root in roots],
        "files": files,
    }


def ensure_oracle_compatibility(
    resolved_version: str,
    *,
    package_root: Path | None = None,
    backup_root: Path | None = None,
) -> dict[str, Any]:
    version = resolved_version.strip().removeprefix("oracle ").strip()
    contracts = VERSION_PATCHES.get(version)
    if contracts is None:
        raise OracleCompatError(
            "ORACLE_VERSION_UNVALIDATED",
            "Oracle compatibility is validated only for tested versions",
            {"resolved": resolved_version, "supported": list(RECOVERABLE_VERSIONS)},
        )
    roots = resolve_package_roots(version) if package_root is None else [package_root.expanduser().resolve(strict=True)]
    patches = patch_root(version)
    backup = backup_root or (Path.home() / ".codex" / "state" / "oracle-compat-backups" / version)
    changed: list[str] = []
    already: list[str] = []
    for root in roots:
        if package_version(root) != version:
            raise OracleCompatError("ORACLE_VERSION_MISMATCH", "Oracle package version does not match the resolved CLI version")
        for relative, contract in contracts.items():
            target = root / Path(relative)
            current = sha256_file(target)
            item = relative if len(roots) == 1 else f"{root}:{relative}"
            if current == contract["patched"]:
                already.append(item)
                continue
            backup_path = backup / Path(relative)
            if current in contract.get("legacy_patched", []):
                if not backup_path.exists() or sha256_file(backup_path) != contract["pristine"]:
                    legacy_patches = contract.get("legacy_patches")
                    legacy_patch = (
                        legacy_patches.get(current)
                        if isinstance(legacy_patches, dict)
                        else None
                    ) or contract.get("legacy_patch")
                    if isinstance(legacy_patch, str):
                        patch_paths = [patches / legacy_patch]
                    elif isinstance(legacy_patch, list) and legacy_patch and all(
                        isinstance(item, str) and item for item in legacy_patch
                    ):
                        patch_paths = [patches / item for item in legacy_patch]
                    else:
                        raise OracleCompatError(
                            "ORACLE_LEGACY_PATCH_BACKUP_INVALID",
                            "A legacy Oracle patch cannot be migrated without the exact pristine backup",
                            {"path": str(target), "backup": str(backup_path), "actual": current},
                        )
                    _migrate_known_legacy_patch(
                        root, target, relative, patch_paths, str(contract["pristine"])
                    )
                    backup_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, backup_path)
                else:
                    shutil.copy2(backup_path, target)
                current = sha256_file(target)
            if current != contract["pristine"]:
                raise OracleCompatError(
                    "ORACLE_FILE_HASH_MISMATCH",
                    "Oracle compatibility refuses an unknown third-party file",
                    {"path": str(target), "actual": current, "expected": [contract["pristine"], contract["patched"]]},
                )
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            if not backup_path.exists():
                shutil.copy2(target, backup_path)
            _apply_patch(root, patches / str(contract["patch"]))
            actual = sha256_file(target)
            if actual != contract["patched"]:
                raise OracleCompatError(
                    "ORACLE_PATCH_HASH_MISMATCH",
                    "Oracle compatibility patch output hash is unexpected",
                    {"path": str(target), "actual": actual, "expected": contract["patched"]},
                )
            changed.append(item)
    return {
        "ok": True,
        "version": version,
        "package_root": str(roots[0]),
        "package_roots": [str(root) for root in roots],
        "changed": changed,
        "already_patched": already,
    }


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=f"Apply the exact Oracle {SUPPORTED_VERSION} ChatGPT compatibility patch.")
    parser.add_argument("--resolved-version", default=f"oracle {SUPPORTED_VERSION}")
    parser.add_argument("--package-root", type=Path)
    args = parser.parse_args(argv)
    try:
        result = ensure_oracle_compatibility(args.resolved_version, package_root=args.package_root)
    except OracleCompatError as exc:
        result = {"ok": False, "error": {"code": exc.code, "message": str(exc), "evidence": exc.evidence}}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
