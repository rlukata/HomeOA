from django.utils.translation import gettext_lazy as _
from pydantic import BaseModel, Field

from homeoa.plugins.manifest import PluginManifest


class TesseractSettings(BaseModel):
    language: str = Field(
        "eng",
        title=_("Language"),
        description=_(
            "Tesseract language code(s) of the documents, e.g. eng, or eng+spa. Each needs "
            "its language data installed on the server."
        ),
        pattern=r"^[a-z_]{3,8}(\+[a-z_]{3,8})*$",
    )


manifest = PluginManifest(
    id="tesseract",
    name=_("Tesseract OCR"),
    version="0.1.0",
    core_requires=">=0.1,<0.2",
    settings_model=TesseractSettings,
    providers={"OcrProvider": "homeoa_tesseract.provider.TesseractOcr"},
    description=_(
        "Reads scanned PDF pages and photos on this server, so their text can be searched. "
        "Nothing is sent anywhere. Needs the tesseract program installed on the server."
    ),
)
