"""Package the approved pixel-art master into Windows icon formats."""
from pathlib import Path
from PIL import Image
out=Path(__file__).parent/'assets'
master=Image.open(out/'mechanicus-pixel-preview.png').convert('RGBA')
# Preserve approved artwork; only produce application-sized representations.
sizes=[16,24,32,48,64,128,256]
images=[master.resize((n,n),Image.Resampling.LANCZOS) for n in sizes]
images[-1].save(out/'ats-icon.png')
images[-1].save(out/'ats-icon.ico',sizes=[(n,n) for n in sizes],append_images=images[:-1])
print('ICON_CREATED_FROM_APPROVED_MASTER')
