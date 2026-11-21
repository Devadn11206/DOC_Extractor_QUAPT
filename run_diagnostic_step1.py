import sys, os, io, json
sys.path.insert(0, 'src')
sys.stdout.reconfigure(encoding='utf-8')
import cv2
import numpy as np

img_path = 'C:/Users/ddnan/.gemini/antigravity/brain/8f7911e7-56eb-4264-8921-aad1a2011ba0/.user_uploaded/media_1794109987840_646d3121.jpg'
raw_bytes = open(img_path, 'rb').read()

print('=== STEP 1: INGESTION & RENDERING VERIFICATION ===')
from doc_extractor.pipeline import ModuleAPipeline
ma = ModuleAPipeline()
stream = io.BytesIO(raw_bytes)
handoff = ma.process_document(stream, 'test_account_form.jpg')

print(f'Handoff Document ID: {handoff.document_id}')
print(f'Handoff Status: {handoff.status}')
print(f'Page Count: {handoff.page_count}')

page_record = handoff.pages[0]
orig_img_path = ma.storage.resolve_uri_to_path(page_record.original_storage_uri)
prep_img_path = ma.storage.resolve_uri_to_path(page_record.preprocessed_storage_uri)

orig_mat = cv2.imread(str(orig_img_path))
prep_mat = cv2.imread(str(prep_img_path))

print(f'Rendered/Original Image: shape={orig_mat.shape}, dtype={orig_mat.dtype}, channels={orig_mat.shape[2]}')
print(f'Preprocessed Image: shape={prep_mat.shape}, dtype={prep_mat.dtype}, channels={prep_mat.shape[2]}')
print(f'Metadata sidecar: rotation={page_record.applied_rotation_deg}, deskew={page_record.applied_deskew_deg}, contrast={page_record.contrast_method}')

# Save preprocessed image to disk for inspection
diag_prep_path = 'diagnostic_preprocessed.png'
cv2.imwrite(diag_prep_path, prep_mat)
print(f'Saved preprocessed diagnostic image to: {diag_prep_path} (exists: {os.path.exists(diag_prep_path)})')

# Check pixel stats: did CLAHE or preprocessing threshold or wash out the text?
print(f'Original pixel mean={orig_mat.mean():.2f}, std={orig_mat.std():.2f}, min={orig_mat.min()}, max={orig_mat.max()}')
print(f'Preprocessed pixel mean={prep_mat.mean():.2f}, std={prep_mat.std():.2f}, min={prep_mat.min()}, max={prep_mat.max()}')

