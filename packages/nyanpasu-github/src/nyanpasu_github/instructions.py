from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from nyanpasu.models import InstructionDocument

if TYPE_CHECKING:
    from nyanpasu_github.models import GitHubRepoSettings, InstructionDocumentSettings


def instruction_document_from_settings(setting: InstructionDocumentSettings) -> InstructionDocument | None:
    if setting.content is not None:
        return InstructionDocument(
            name=setting.name or "inline-instructions",
            content=setting.content,
            source=None,
        )
    if setting.path is None:
        return None
    try:
        content = setting.path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if setting.required:
            raise
        logger.warning("instruction document skipped missing path={}", setting.path)
        return None
    return InstructionDocument(
        name=setting.name or setting.path.name,
        content=content,
        source=str(setting.path),
    )


def instruction_documents_for_repo(
    *,
    repo: str,
    plugin_instruction_docs: tuple[InstructionDocumentSettings, ...],
    repo_settings: dict[str, GitHubRepoSettings],
) -> tuple[InstructionDocument, ...]:
    settings = list(plugin_instruction_docs)
    if repo_config := repo_settings.get(repo):
        settings.extend(repo_config.instruction_docs)
    docs: list[InstructionDocument] = []
    for setting in settings:
        doc = instruction_document_from_settings(setting)
        if doc is not None:
            docs.append(doc)
    return tuple(docs)
