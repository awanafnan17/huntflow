import os
from django.conf import settings
from django.core.files.storage import FileSystemStorage

class PrivateFileSystemStorage(FileSystemStorage):
    def __init__(self, *args, **kwargs):
        # Store in private media root defined in settings
        location = settings.PRIVATE_MEDIA_ROOT
        super().__init__(location=location, base_url=None, *args, **kwargs)
