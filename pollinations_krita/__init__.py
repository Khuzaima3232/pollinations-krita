"""Pollinations for Krita: generate an image onto a new layer, or edit a layer.

Network work runs on a worker thread and results are handed back to the UI
thread, so Krita never freezes while an image is being generated.
"""
import os
import tempfile
import threading

from krita import Extension, Krita, InfoObject  # type: ignore[import-not-found]
from PyQt5.QtCore import QThread, pyqtSignal  # type: ignore[import-not-found]
from PyQt5.QtWidgets import (  # type: ignore[import-not-found]
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
)

from . import pollinations_api as api
from .token_store import TokenStore

SETTING_MODEL = "pollinations/model"
SETTING_WIDTH = "pollinations/width"
SETTING_HEIGHT = "pollinations/height"


class GenerateWorker(QThread):
    """Runs one generation off the UI thread and reports the result back."""

    finished_ok = pyqtSignal(bytes)
    failed = pyqtSignal(str)

    def __init__(self, token, model, prompt, width, height, source):
        super().__init__()
        self._args = (token, model, prompt, width, height, source)

    def run(self):
        try:
            image = api.generate(*self._args)
            self.finished_ok.emit(image)
        except api.ApiError as error:
            self.failed.emit(str(error))
        except Exception as error:  # never crash Krita from a worker
            self.failed.emit("Unexpected error: %s" % error)


class AuthorizeWorker(QThread):
    """Runs the device-flow poll off the UI thread."""

    authorized = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, code, cancel):
        super().__init__()
        self._code = code
        self._cancel = cancel

    def run(self):
        try:
            self.authorized.emit(api.poll_authorization(self._code, self._cancel))
        except api.ApiError as error:
            self.failed.emit(str(error))


def _active_document():
    return Krita.instance().activeDocument()


def _active_node():
    document = _active_document()
    if document is None:
        return None
    return document.activeNode()


def _layer_png(node):
    """PNG bytes of a node, for use as an edit source."""
    document = _active_document()
    if document is None or node is None:
        return None
    path = os.path.join(tempfile.gettempdir(), "pollinations-source.png")
    try:
        # Krita exports the node itself, so no selection maths is needed.
        node.save(path, document.width(), document.height(), document.resolution(), InfoObject())
        with open(path, "rb") as handle:
            return handle.read()
    except Exception:
        return None
    finally:
        if os.path.exists(path):
            try:
                os.unlink(path)
            except OSError:
                pass


def _place_as_layer(png_bytes, name):
    """Make a new paint layer holding the generated image and select it."""
    document = _active_document()
    if document is None:
        raise api.ApiError("Open a document first.")

    path = os.path.join(tempfile.gettempdir(), "pollinations-result.png")
    with open(path, "wb") as handle:
        handle.write(png_bytes)
    try:
        document.setBatchmode(True)
        layer = document.createNode(name, "paintlayer")
        document.rootNode().addChildNode(layer, None)
        # Load the pixels into the new layer, then scale it to the canvas.
        success = layer.load(path)
        if not success:
            raise api.ApiError("Krita could not load the generated image.")
        if layer.bounds().width() != document.width() or layer.bounds().height() != document.height():
            layer.scaleNode(
                None,
                document.width(),
                document.height(),
                InfoObject(),
            )
        document.refreshProjection()
    finally:
        document.setBatchmode(False)
        if os.path.exists(path):
            try:
                os.unlink(path)
            except OSError:
                pass
    return layer


class PollinationsDialog(QDialog):
    """Prompt, model picker and resolution, then generate or edit."""

    def __init__(self, parent=None, edit=False):
        super().__init__(parent)
        self.setWindowTitle("Pollinations — %s" % ("Edit layer" if edit else "Generate image"))
        self.setMinimumWidth(460)
        self.edit = edit
        self.token = TokenStore().load()
        self.models = []
        self.worker = None

        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)

        self.prompt = QTextEdit()
        self.prompt.setPlaceholderText("Describe the image")
        self.prompt.setMinimumHeight(90)
        form.addRow(QLabel("Prompt"), self.prompt)

        self.model = QComboBox()
        self.model.setEditable(True)
        form.addRow(QLabel("Model"), self.model)

        self.width = QSpinBox()
        self.width.setRange(64, 4096)
        self.width.setValue(int(Krita.instance().readSetting(SETTING_WIDTH, "1024") or 1024))
        self.height = QSpinBox()
        self.height.setRange(64, 4096)
        self.height.setValue(int(Krita.instance().readSetting(SETTING_HEIGHT, "1024") or 1024))
        size = QFormLayout()
        size.addRow(QLabel("Width"), self.width)
        size.addRow(QLabel("Height"), self.height)
        layout.addLayout(size)

        self.use_selection = QCheckBox("Use the current selection size")
        self.use_selection.setChecked(False if edit else True)
        layout.addWidget(self.use_selection)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        buttons = QDialogButtonBox()
        self.connect_button = QPushButton("Connect account")
        self.go_button = QPushButton("Generate")
        buttons.addButton(self.connect_button, QDialogButtonBox.ActionRole)
        buttons.addButton(self.go_button, QDialogButtonBox.AcceptRole)
        buttons.addButton(QDialogButtonBox.Cancel)
        layout.addWidget(buttons)

        self.connect_button.clicked.connect(self.connect_account)
        self.go_button.clicked.connect(self.start)
        buttons.rejected.connect(self.reject)

        if not self.token:
            self.status.setText("Not connected. Use Connect account first.")
            self.go_button.setEnabled(False)

        self.load_models()

    def load_models(self):
        if not self.token:
            return
        try:
            self.models = api.load_image_models(self.token)
        except api.ApiError as error:
            self.status.setText(str(error))
            return
        current = Krita.instance().readSetting(SETTING_MODEL, "") or ""
        for model in self.models:
            self.model.addItem(model["name"])
        if current:
            index = self.model.findText(current)
            if index >= 0:
                self.model.setCurrentIndex(index)

    def connect_account(self):
        try:
            code = api.begin_authorization()
        except api.ApiError as error:
            QMessageBox.warning(self, "Pollinations", str(error))
            return
        self.status.setText("Approve code %s in your browser." % code["user_code"])
        self._cancel = threading.Event()
        self._auth_worker = AuthorizeWorker(code, self._cancel)
        self._auth_worker.authorized.connect(self._authorized)
        self._auth_worker.failed.connect(self._auth_failed)
        self._auth_worker.start()
        try:
            import webbrowser

            webbrowser.open(code["approval_url"])
        except Exception:
            self.status.setText(
                "Open this URL and approve %s: %s" % (code["user_code"], code["approval_url"])
            )

    def _authorized(self, token):
        try:
            TokenStore().save(token)
        except (OSError, ValueError) as error:
            self.status.setText("Could not store the authorization: %s" % error)
            return
        self.token = token
        self.status.setText("Connected.")
        self.go_button.setEnabled(True)
        self.load_models()

    def _auth_failed(self, message):
        self.status.setText(message)

    def start(self):
        prompt = self.prompt.toPlainText().strip()
        if not prompt:
            self.status.setText("Enter a prompt.")
            return
        model_name = self.model.currentText().strip()
        model = next((m for m in self.models if m["name"] == model_name), None)
        if model is None:
            self.status.setText("Choose a model from the list.")
            return

        source = None
        if self.edit:
            source = _layer_png(_active_node())
            if source is None:
                self.status.setText("Could not read the active layer.")
                return
            if not api.supports_image_input(model):
                self.status.setText("This model does not accept image input. Pick another.")
                return

        Krita.instance().writeSetting(SETTING_MODEL, model["name"])
        Krita.instance().writeSetting(SETTING_WIDTH, str(self.width.value()))
        Krita.instance().writeSetting(SETTING_HEIGHT, str(self.height.value()))

        self.go_button.setEnabled(False)
        self.status.setText("Generating…")
        self.worker = GenerateWorker(
            self.token,
            model,
            prompt,
            self.width.value(),
            self.height.value(),
            source,
        )
        self.worker.finished_ok.connect(self._generated)
        self.worker.failed.connect(self._generation_failed)
        self.worker.start()

    def _generated(self, png_bytes):
        try:
            _place_as_layer(png_bytes, "Pollinations")
        except api.ApiError as error:
            self.status.setText(str(error))
            self.go_button.setEnabled(True)
            return
        self.status.setText("Done: added as a new layer.")
        self.go_button.setEnabled(True)
        self.accept()

    def _generation_failed(self, message):
        self.status.setText(message)
        self.go_button.setEnabled(True)


class PollinationsExtension(Extension):
    def __init__(self, parent):
        super().__init__(parent)

    def setup(self):
        pass

    def createActions(self, window):
        generate = window.createAction("pollinations_generate", "Pollinations…", "tools/scripts")
        generate.triggered.connect(lambda: PollinationsDialog(window.qwindow()).exec_())
        edit = window.createAction("pollinations_edit", "Pollinations: edit layer…", "tools/scripts")
        edit.triggered.connect(
            lambda: PollinationsDialog(window.qwindow(), edit=True).exec_()
        )


Krita.instance().addExtension(PollinationsExtension(Krita.instance()))
