const { Plugin, Notice, MarkdownView } = require("obsidian");
const { execFile } = require("child_process");
const fs = require("fs/promises");
const path = require("path");

const USER_HOME = process.env.USERPROFILE || process.env.HOME || "";
const DOWNLOAD_DIR = path.join(USER_HOME, "Downloads", "Obsidian_pdf");
const MPDF_DIR = path.join(USER_HOME, "Desktop", "md2pdf");
const MPDF_BAT = path.join(MPDF_DIR, "mpdf.bat");
const MPDF_PY = path.join(MPDF_DIR, "mpdf.py");

module.exports = class MpdfExportPlugin extends Plugin {
	onload() {
		const ribbonIcon = this.addRibbonIcon(
			"download",
			"Export active note to Downloads as PDF",
			() => this.exportActiveNote(),
		);
		ribbonIcon.addClass("mpdf-export-ribbon");

		this.addCommand({
			id: "export-active-note-to-downloads",
			name: "Export active note to Downloads as PDF",
			callback: () => this.exportActiveNote(),
		});
	}

	async exportActiveNote() {
		const file = this.app.workspace.getActiveFile();
		if (!file) {
			new Notice("Open a Markdown note first.");
			return;
		}

		if (file.extension !== "md") {
			new Notice("Only Markdown notes can be exported.");
			return;
		}

		const activeView = this.app.workspace.getActiveViewOfType(MarkdownView);
		if (activeView) {
			await activeView.save();
		}

		const vaultPath = this.app.vault.adapter.basePath;
		const inputPath = path.join(vaultPath, file.path);
		const generatedPdfPath = path.join(path.dirname(inputPath), `${file.basename}.pdf`);
		const downloadPdfPath = path.join(DOWNLOAD_DIR, `${file.basename}.pdf`);

		new Notice("Exporting PDF to Downloads...");

		try {
			await fs.mkdir(DOWNLOAD_DIR, { recursive: true });
			await this.runMpdf(inputPath);
			await fs.copyFile(generatedPdfPath, downloadPdfPath);

			if (generatedPdfPath !== downloadPdfPath) {
				await fs.unlink(generatedPdfPath);
			}

			new Notice(`PDF exported to ${downloadPdfPath}`);
		} catch (error) {
			console.error("MPDF export failed", error);
			new Notice(`Export failed: ${formatError(error)}`);
		}
	}

	async runMpdf(inputPath) {
		if (await pathExists(MPDF_BAT)) {
			await execFileAsync("cmd.exe", ["/c", MPDF_BAT, inputPath]);
			return;
		}

		if (await pathExists(MPDF_PY)) {
			await execFileAsync("py", ["-3", MPDF_PY, inputPath]);
			return;
		}

		await execFileAsync("cmd.exe", ["/c", "mpdf", inputPath]);
	}
};

async function pathExists(targetPath) {
	try {
		await fs.access(targetPath);
		return true;
	} catch {
		return false;
	}
}

function execFileAsync(command, args) {
	return new Promise((resolve, reject) => {
		execFile(command, args, { windowsHide: true }, (error, stdout, stderr) => {
			if (error) {
				error.stdout = stdout;
				error.stderr = stderr;
				reject(error);
				return;
			}

			resolve({ stdout, stderr });
		});
	});
}

function formatError(error) {
	if (!error) {
		return "Unknown error";
	}

	if (typeof error.stderr === "string" && error.stderr.trim()) {
		return error.stderr.trim();
	}

	if (typeof error.message === "string" && error.message.trim()) {
		return error.message.trim();
	}

	return String(error);
}
