"""Utilitários compartilhados entre apps — módulo simples, sem models/views
próprios, não é um app Django instalado (não entra em INSTALLED_APPS)."""

import io

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import InMemoryUploadedFile, TemporaryUploadedFile
from PIL import Image

MAX_IMAGE_DIMENSION = 1600
JPEG_QUALITY = 82


def compress_uploaded_image(field_file, *, max_dimension=MAX_IMAGE_DIMENSION, quality=JPEG_QUALITY):
    """Redimensiona (só encolhe, mantém proporção) e recomprime uma imagem
    recém-enviada, antes de gravar no storage — hoje nenhum upload (logo,
    capa, fundo, foto de responsável/funcionário) passa por limite de
    tamanho, então uma foto de celular sem edição vai inteira pro disco
    (e pro backup offsite) sem necessidade nenhuma pro que é exibido.

    Só processa upload NOVO — identificado pelo tipo do arquivo em memória
    (`InMemoryUploadedFile`/`TemporaryUploadedFile`, o que o form usa pra um
    arquivo recém-enviado). Um `FieldFile` já salvo, sem mudança nesse save,
    é ignorado — não reprocessa a cada save() do model.

    Preserva o formato original (JPEG continua JPEG, PNG continua PNG) —
    forçar tudo pra JPEG quebraria transparência de logo em PNG.
    """
    if not field_file or not isinstance(
        getattr(field_file, "file", None), (InMemoryUploadedFile, TemporaryUploadedFile)
    ):
        return

    image = Image.open(field_file)
    image_format = (image.format or "JPEG").upper()
    original_name = field_file.name

    image.thumbnail((max_dimension, max_dimension), Image.LANCZOS)

    save_kwargs = {"optimize": True}
    if image_format in ("JPEG", "JPG"):
        # JPEG não suporta canal alpha — achata sobre fundo branco se a
        # imagem de origem tinha transparência (ex: PNG salvo com .jpg).
        if image.mode in ("RGBA", "LA", "P"):
            image = image.convert("RGB")
        save_kwargs["quality"] = quality

    buffer = io.BytesIO()
    image.save(buffer, format=image_format, **save_kwargs)
    field_file.save(original_name, ContentFile(buffer.getvalue()), save=False)
