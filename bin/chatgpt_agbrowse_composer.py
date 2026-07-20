from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Mapping


APP_PRIMITIVES_PATH = Path(__file__).resolve().with_name("codexpro_agbrowse_app.py")
SELECTION_SCHEMA = "codex.chatgpt.capability-selection/v1"
RESEARCH_TOKEN = "@심층 리서치"
COMPOSER_URL = "https://chatgpt.com/"
RESEARCH_NAMES = {"심층 리서치", "deep research"}
EXPLICIT_SELECTION_VALUES = {"true", "checked", "pressed", "selected", "current"}


def _load_primitives():
    spec = importlib.util.spec_from_file_location(
        "codexpro_agbrowse_primitives_research",
        APP_PRIMITIVES_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"agbrowse primitives unavailable: {APP_PRIMITIVES_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PRIMITIVES = _load_primitives()
AgbrowseGateway = PRIMITIVES.AgbrowseGateway


class ResearchComposerError(RuntimeError):
    def __init__(self, code: str, message: str, evidence: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.evidence = dict(evidence or {})


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _snapshot_projection(page: Any) -> dict[str, Any]:
    return {
        "url": str(page.url),
        "target_id": str(page.target_id),
        "text_summary": str(page.text_summary),
        "nodes": [
            {
                "ref": str(node.ref),
                "role": str(node.role),
                "name": str(node.name),
                "value": str(node.value),
                "checked": node.checked,
            }
            for node in page.nodes
        ],
    }


def _unique_composer_textbox(page: Any) -> Any:
    names = {
        "chatgpt와 채팅",
        "message chatgpt",
        "ask chatgpt",
        "chat with chatgpt",
    }
    matches = [
        node
        for node in page.nodes
        if node.role == "textbox" and node.name.casefold() in names
    ]
    if len(matches) != 1:
        raise ResearchComposerError(
            "RESEARCH_COMPOSER_AMBIGUOUS",
            "research selection requires one exact ChatGPT composer textbox",
            {"match_count": len(matches), "target_id": page.target_id, "url": page.url},
        )
    return matches[0]


def _is_research_marker(node: Any) -> bool:
    return node.name.casefold() in RESEARCH_NAMES


def _marker_identity(node: Any) -> tuple[str, str, str, bool | None]:
    """A snapshot-stable marker identity; refs are only snapshot-local capabilities."""
    return (node.role, node.name, node.value, node.checked)


def _is_explicitly_selected(node: Any) -> bool:
    return node.checked is True or node.value.casefold() in EXPLICIT_SELECTION_VALUES


def _marker_payload(node: Any) -> dict[str, Any]:
    return {"role": node.role, "name": node.name, "checked": node.checked}


def _selection_proof(before: Any, after: Any) -> tuple[Any, dict[str, Any]]:
    """Require explicit state, or the exact token action's unique new capability pill."""
    explicit = [node for node in after.nodes if _is_research_marker(node) and _is_explicitly_selected(node)]
    if len(explicit) == 1:
        return explicit[0], {"kind": "explicit-state", "marker": _marker_payload(explicit[0])}
    if len(explicit) > 1:
        raise ResearchComposerError(
            "DEEP_RESEARCH_CAPABILITY_UNPROVEN",
            "post-Tab snapshot contained ambiguous explicit Deep Research selection state",
            {
                "explicit_match_count": len(explicit),
                "transition_candidate_count": 0,
                "target_id": after.target_id,
                "url": after.url,
                "accepted_names": sorted(RESEARCH_NAMES),
            },
        )

    before_markers = {
        _marker_identity(node) for node in before.nodes if _is_research_marker(node)
    }
    transitioned_pills = [
        node
        for node in after.nodes
        if _is_research_marker(node)
        and node.role == "button"
        and _marker_identity(node) not in before_markers
    ]
    if len(transitioned_pills) == 1:
        marker = transitioned_pills[0]
        return marker, {
            "kind": "token-to-pill-transition",
            "marker": _marker_payload(marker),
            "marker_identity_sha256": _sha256(_marker_identity(marker)),
        }

    raise ResearchComposerError(
        "DEEP_RESEARCH_CAPABILITY_UNPROVEN",
        "post-Tab snapshot did not prove selected Deep Research state or a unique token-to-pill transition",
        {
            "explicit_match_count": len(explicit),
            "transition_candidate_count": len(transitioned_pills),
            "target_id": after.target_id,
            "url": after.url,
            "accepted_names": sorted(RESEARCH_NAMES),
        },
    )


class ResearchComposer:
    def __init__(self, gateway: Any):
        self.ui = gateway
        self._transition_proofs: dict[str, str] = {}

    def restore_selection_evidence(self, evidence: Mapping[str, Any]) -> None:
        """Rehydrate only an immutable transition proof from a prior process."""
        proof = evidence.get("selection_proof") if isinstance(evidence.get("selection_proof"), dict) else {}
        target_id = str(evidence.get("target_id") or "")
        app_name = str(evidence.get("app_name") or "").strip()
        app_mention_hash = str(evidence.get("app_mention_text_sha256") or "")
        marker_hash = str(proof.get("marker_identity_sha256") or "")
        evidence_hashes = ("token_sha256", "before_snapshot_sha256", "after_snapshot_sha256", "action_transcript_sha256")
        exact_hashes = all(
            len(str(evidence.get(key) or "")) == 64
            and all(character in "0123456789abcdef" for character in str(evidence.get(key) or ""))
            for key in evidence_hashes
        )
        proof_hashes_match = all(
            str(proof.get(key) or "") == str(evidence.get(key) or "")
            for key in evidence_hashes
        )
        if not (
            evidence.get("schema") == SELECTION_SCHEMA
            and evidence.get("state") == "deep-research-selected"
            and proof.get("kind") == "token-to-pill-transition"
            and target_id
            and app_name
            and evidence.get("app_selection_method") == "exact-at-mention-then-tab"
            and app_mention_hash == hashlib.sha256(f"@{app_name}".encode("utf-8")).hexdigest()
            and len(marker_hash) == 64
            and all(character in "0123456789abcdef" for character in marker_hash)
            and str(proof.get("token_sha256") or "")
            == hashlib.sha256(RESEARCH_TOKEN.encode("utf-8")).hexdigest()
            and exact_hashes
            and proof_hashes_match
        ):
            raise ResearchComposerError(
                "RESEARCH_SELECTION_EVIDENCE_INVALID",
                "saved Deep Research transition evidence cannot be rehydrated",
                {"target_id": target_id},
            )
        self._transition_proofs[target_id] = marker_hash

    def prepare(
        self,
        *,
        run_id: str,
        workflow_id: str,
        app_name: str,
        composer_url: str = COMPOSER_URL,
    ) -> dict[str, Any]:
        app_name = str(app_name or "").strip()
        if not run_id or not workflow_id or not app_name:
            raise ResearchComposerError(
                "RESEARCH_SELECTION_IDENTITY_MISSING",
                "run_id, workflow_id, and the exact app name are required before research selection",
            )
        if not composer_url.startswith("https://chatgpt.com/"):
            raise ResearchComposerError(
                "RESEARCH_COMPOSER_URL_INVALID",
                "research composer URL must use the exact https://chatgpt.com/ origin",
            )

        self.ui.ensure_started()
        created = self.ui.new_tab(composer_url)
        target_id = str(created.get("targetId") or created.get("target_id") or "")
        if not target_id:
            raise ResearchComposerError(
                "RESEARCH_COMPOSER_TARGET_MISSING",
                "new research composer did not return a target id",
            )
        actions: list[dict[str, Any]] = [
            {"action": "new-tab", "target_id": target_id, "url": composer_url}
        ]
        try:
            self.ui.activate_target(target_id)
            actions.append({"action": "activate", "target_id": target_id})
            settle = getattr(self.ui, "settle", None)
            if callable(settle):
                settle()
            before = self.ui.snapshot()
            actions.append({"action": "snapshot-before", "target_id": before.target_id})
            if before.target_id != target_id:
                raise ResearchComposerError(
                    "RESEARCH_COMPOSER_TARGET_MISMATCH",
                    "pre-selection snapshot belonged to a foreign target",
                    {"expected_target_id": target_id, "actual_target_id": before.target_id},
                )
            if not before.url.startswith("https://chatgpt.com/") or "#settings" in before.url.casefold():
                raise ResearchComposerError(
                    "RESEARCH_COMPOSER_ROUTE_INVALID",
                    "pre-selection target is not a ChatGPT composer route",
                    {"target_id": target_id, "url": before.url},
                )
            app_mention = f"@{app_name}"
            textbox = _unique_composer_textbox(before)
            self.ui.type(textbox, app_mention)
            actions.append(
                {
                    "action": "type-app",
                    "target_id": target_id,
                    "ref": textbox.ref,
                    "text_sha256": hashlib.sha256(app_mention.encode("utf-8")).hexdigest(),
                }
            )
            self.ui.press("Tab")
            actions.append({"action": "press-app", "key": "Tab", "target_id": target_id})
            if callable(settle):
                settle()
            before_research = self.ui.snapshot()
            actions.append({"action": "snapshot-before-research", "target_id": before_research.target_id})
            if before_research.target_id != target_id:
                raise ResearchComposerError(
                    "RESEARCH_COMPOSER_TARGET_MISMATCH",
                    "post-app snapshot belonged to a foreign target",
                    {"expected_target_id": target_id, "actual_target_id": before_research.target_id},
                )
            textbox = _unique_composer_textbox(before_research)
            self.ui.type(textbox, RESEARCH_TOKEN)
            actions.append(
                {
                    "action": "type",
                    "target_id": target_id,
                    "ref": textbox.ref,
                    "text_sha256": hashlib.sha256(RESEARCH_TOKEN.encode("utf-8")).hexdigest(),
                }
            )
            self.ui.press("Tab")
            actions.append({"action": "press", "key": "Tab", "target_id": target_id})
            if callable(settle):
                settle()
            after = self.ui.snapshot()
            actions.append({"action": "snapshot-after", "target_id": after.target_id})
            if after.target_id != target_id:
                raise ResearchComposerError(
                    "RESEARCH_COMPOSER_TARGET_MISMATCH",
                    "post-selection snapshot belonged to a foreign target",
                    {"expected_target_id": target_id, "actual_target_id": after.target_id},
                )
            before_projection = _snapshot_projection(before_research)
            after_projection = _snapshot_projection(after)
            marker, selection_proof = _selection_proof(before_research, after)
            if selection_proof["kind"] == "token-to-pill-transition":
                self._transition_proofs[target_id] = str(selection_proof["marker_identity_sha256"])
            selection_proof = {
                **selection_proof,
                "token_sha256": hashlib.sha256(RESEARCH_TOKEN.encode("utf-8")).hexdigest(),
                "before_snapshot_sha256": _sha256(before_projection),
                "after_snapshot_sha256": _sha256(after_projection),
                "action_transcript_sha256": _sha256(actions),
            }
            return {
                "schema": SELECTION_SCHEMA,
                "state": "deep-research-selected",
                "run_id": run_id,
                "workflow_id": workflow_id,
                "app_name": app_name,
                "app_selection_method": "exact-at-mention-then-tab",
                "app_mention_text_sha256": hashlib.sha256(app_mention.encode("utf-8")).hexdigest(),
                "session_id": None,
                "target_id": target_id,
                "url": after.url,
                "selection_transport": "preselected-research",
                "token_sha256": hashlib.sha256(RESEARCH_TOKEN.encode("utf-8")).hexdigest(),
                "before_snapshot_sha256": _sha256(before_projection),
                "after_snapshot_sha256": _sha256(after_projection),
                "action_transcript_sha256": _sha256(actions),
                "selected_marker": _marker_payload(marker),
                "selection_proof": selection_proof,
                "action_count": len(actions),
            }
        except ResearchComposerError as exc:
            exc.evidence = {
                **exc.evidence,
                "owned_target_id": target_id,
                "owned_stage": "pre-submit-research-composer",
                "action_transcript_sha256": _sha256(actions),
            }
            raise
        except Exception as exc:
            raise ResearchComposerError(
                "RESEARCH_COMPOSER_INTERNAL",
                "research composer preparation failed",
                {
                    "owned_target_id": target_id,
                    "owned_stage": "pre-submit-research-composer",
                    "action_transcript_sha256": _sha256(actions),
                },
            ) from exc

    def activate_target(self, target_id: str) -> dict[str, Any]:
        return self.ui.activate_target(target_id)

    def verify_selected(self, target_id: str) -> dict[str, Any]:
        self.ui.activate_target(target_id)
        settle = getattr(self.ui, "settle", None)
        if callable(settle):
            settle()
        page = self.ui.snapshot()
        if page.target_id != target_id:
            raise ResearchComposerError(
                "RESEARCH_COMPOSER_TARGET_MISMATCH",
                "final research selection check belonged to a foreign target",
                {"expected_target_id": target_id, "actual_target_id": page.target_id},
            )
        explicit = [node for node in page.nodes if _is_research_marker(node) and _is_explicitly_selected(node)]
        if len(explicit) == 1:
            marker = explicit[0]
            proof_kind = "explicit-state"
        else:
            if len(explicit) > 1:
                raise ResearchComposerError(
                    "DEEP_RESEARCH_CAPABILITY_UNPROVEN",
                    "final snapshot contained ambiguous explicit Deep Research selection state",
                    {"explicit_match_count": len(explicit), "target_id": target_id, "url": page.url},
                )
            trusted_identity_hash = self._transition_proofs.get(target_id)
            transitioned = [
                node
                for node in page.nodes
                if _is_research_marker(node)
                and _sha256(_marker_identity(node)) == trusted_identity_hash
            ]
            if trusted_identity_hash is None or len(transitioned) != 1:
                raise ResearchComposerError(
                    "DEEP_RESEARCH_CAPABILITY_UNPROVEN",
                    "final snapshot did not retain explicit selection state or this composer's proven token-to-pill transition",
                    {
                        "explicit_match_count": len(explicit),
                        "trusted_transition": trusted_identity_hash is not None,
                        "transition_match_count": len(transitioned),
                        "target_id": target_id,
                        "url": page.url,
                    },
                )
            marker = transitioned[0]
            proof_kind = "token-to-pill-transition"
        return {
            "schema": "codex.chatgpt.capability-selection-final-check/v1",
            "state": "deep-research-selected",
            "target_id": target_id,
            "url": page.url,
            "snapshot_sha256": _sha256(_snapshot_projection(page)),
            "selected_marker": _marker_payload(marker),
            "selection_proof_kind": proof_kind,
        }
