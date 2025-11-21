"""Provides a command to translate Odoo modules using an LLM."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from odev.common import args, progress
from odev.common.commands import DatabaseCommand
from odev.common.databases.local import LocalDatabase
from odev.common.databases.remote import RemoteDatabase
from odev.common.logging import logging
from odev.common.odoobin import OdoobinProcess

from odev.plugins.odev_plugin_ai.common.llm import LLM
from odev.plugins.odev_plugin_ai.common.odoo_context import OdooContext


logger = logging.getLogger(__name__)


class TranslateCommand(DatabaseCommand):
    """Translates an Odoo module into a specified language using an AI model."""

    _name = "translate"
    _aliases = [
        "trad",
    ]

    lang = args.String(
        aliases=["-l", "--lang"],
        description="ISO Code target language",
    )

    module_name = args.String(
        aliases=["-m", "--module"],
        description="Name of the Odoo module to export",
    )

    path = args.Path(
        aliases=["--path"],
        description="Path to save the translated .po file. Defaults to the current directory.",
        default=Path(".").resolve(),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the command."""
        super().__init__(*args, **kwargs)

    def _get_module_id(self) -> int | None:
        """Search for the module in the database and return its ID."""
        module_ids = self._database.models["ir.module.module"].search([("name", "=", self.args.module_name)], limit=1)
        if not module_ids:
            return logger.error(f"Module '{self.args.module_name}' not found.")

        return module_ids[0]

    def _export_po_file_content(self, module_id: int) -> tuple[str, str] | None:
        """Export the translatable terms of a module for a given language.

        This method uses Odoo's `base.language.export` wizard to generate
        a .po file.

        Args:
            module_id: The database ID of the module to translate.

        Returns:
            A tuple containing the display name (filename) and the base64-encoded
            file content, or None if the export fails.
        """
        # @TODO: Check if lang is installed, handle error if not
        language_export_id = self._database.models["base.language.export"].create(
            {
                "lang": self.args.lang,
                "modules": [(4, module_id)],
                "export_type": "module",
                "format": "po",
            }
        )

        translation_action = self._database.models["base.language.export"].act_getfile(language_export_id)
        if not translation_action or "res_id" not in translation_action:
            logger.error("Failed to trigger the file export action in Odoo.")
            return None

        translation_data = self._database.models["base.language.export"].read(
            [translation_action["res_id"]], fields=["display_name", "data"]
        )
        if not translation_data:
            logger.error("Failed to read the exported translation data from Odoo.")
            return None

        return translation_data[0]["display_name"], translation_data[0]["data"]

    def _get_ai_translation(self, po_content: str) -> str:
        """Send the .po file content to the configured LLM for translation.

        Args:
            po_content: The content of the .po file to be translated.

        Returns:
            The translated content as a string.
        """
        llm = LLM(
            self.config.get("ai", "default_llm"),
            self.store.secrets.get("llm_api_key", scope="api", fields=["password"]).password,
        )

        context = ""

        if isinstance(self._database, RemoteDatabase):
            database = LocalDatabase(self._database.name)
            process = OdoobinProcess(database, version=self._database.version)
            process.with_edition("enterprise")

            if process.check_addons_path(self.args.path):
                process.additional_addons_paths.append(self.args.path)
            if process.check_addon_path(self.args.path):
                process.additional_addons_paths.append(self.args.path.parent)

        elif isinstance(self._database, LocalDatabase):
            process = self._database._get_process_instance()
        else:
            raise TypeError("Unsupported database type for fetching context.")

        process.update_worktrees()
        odoo_context = OdooContext(process)
        context = odoo_context.gather_po_context(po_content)
        prompt_content = f"Here is the PO file content:\n```{po_content}```"

        if context:
            prompt_content += f"\n\nHere is some context to help with the translation:\n```{json.dumps(context)}```"

        logger.debug(f"Calling LLM '{llm.provider}' for translation with prompt: {prompt_content}")

        with progress.spinner(f"Waiting for '{llm.provider}' to complete the translation"):
            ai_translation = llm.completion(
                [
                    {
                        "role": "system",
                        "content": (
                            f"Translate the provided PO file into {self.args.lang} (ISO code)."
                            "Just answer the result merged into the original file without the code block string."
                            "If a context is provided, use it to improve the translation of specific terms."
                        ),
                    },
                    {"role": "user", "content": prompt_content},
                ]
            )

        if not ai_translation:
            raise ValueError("AI translation failed or returned no content.")

        logger.info(f"Translation completed successfully using '{llm.provider}'.")

        return ai_translation

    def _get_output_path(self) -> Path | None:
        """Determine and validate the output path for the translation file.

        It checks if the path exists. If it's an addons path containing the target
        module, it prompts the user to save the translation in the module's `l10n`
        directory.

        Returns:
            The resolved output path, or None if the path is invalid or the user
            declines the prompt.
        """
        output_path = Path(self.args.path)

        if not output_path.exists():
            logger.error(f"The path {output_path} does not exist.")
            return None

        module_path = output_path / self.args.module_name
        if OdoobinProcess.check_addons_path(output_path) and module_path.is_dir():
            if self.console.confirm(
                f"A module folder '{self.args.module_name}' already exists in {output_path}. "
                "Do you want to write the translation file inside its 'i18n' folder?",
                default=True,
            ):
                l10n_path = module_path / "i18n"
                l10n_path.mkdir(exist_ok=True)
                return l10n_path
            logger.info("Operation cancelled by user.")
            return None

        return output_path

    def _write_translation_file(self, path: Path, filename: str, content: str) -> None:
        """Write the translated content to a file.

        Args:
            path: The directory where the file will be saved.
            filename: The name of the file.
            content: The content to write to the file.
        """
        full_path = path / filename
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info(f"Translation file written to {full_path}.")

    def run(self) -> None:
        """Execute the translation process."""
        logger.info(f"Translating '{self.args.module_name}' from {self.args.database} into {self.args.lang}")

        module_id = self._get_module_id()
        if not module_id:
            return

        export_result = self._export_po_file_content(module_id)
        if not export_result:
            return
        filename, b64_content = export_result

        po_content = base64.b64decode(b64_content).decode("utf-8")
        ai_translation = self._get_ai_translation(po_content)

        if ai_translation is None:
            logger.error("AI translation failed.")
            return

        output_path = self._get_output_path()
        if not output_path:
            return

        self._write_translation_file(output_path, filename, ai_translation)
