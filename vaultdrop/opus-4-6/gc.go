package main

import "context"

// RunGC performs garbage collection:
//  1. In a single transaction, find and delete blob records with refcount <= 0.
//  2. For each deleted hash, acquire the per-hash lock, re-check whether a concurrent
//     finalize re-created the record, and only then delete the file.
//
// The per-hash lock coordinates with Finalize: finalize holds the lock while writing
// the blob file and committing the DB record, so GC cannot delete a file that a
// concurrent finalize is about to reference.
func RunGC(ctx context.Context, store *Store, blobMgr *BlobManager) (*GCResponse, error) {
	scanned, err := store.CountBlobs(ctx)
	if err != nil {
		return nil, err
	}

	toDelete, err := store.CollectUnreferencedBlobs(ctx)
	if err != nil {
		return nil, err
	}

	var collected int
	var bytesFreed int64

	for _, b := range toDelete {
		store.LockBlob(b.ContentHash)

		// Re-check: a concurrent finalize may have re-created this blob record
		// between our transaction commit and now.
		exists, err := store.BlobExists(ctx, b.ContentHash)
		if err != nil {
			store.UnlockBlob(b.ContentHash)
			continue
		}
		if exists {
			store.UnlockBlob(b.ContentHash)
			continue
		}

		if err := blobMgr.RemoveBlob(b.ContentHash); err == nil {
			collected++
			bytesFreed += b.Size
		}

		store.UnlockBlob(b.ContentHash)
	}

	return &GCResponse{
		Scanned:    scanned,
		Collected:  collected,
		BytesFreed: bytesFreed,
	}, nil
}

// RunValidation re-hashes every stored blob and flags mismatches.
func RunValidation(ctx context.Context, store *Store, blobMgr *BlobManager) (*ValidateResponse, error) {
	blobs, err := store.ListBlobs(ctx)
	if err != nil {
		return nil, err
	}

	resp := &ValidateResponse{Total: len(blobs)}

	for _, b := range blobs {
		actualHash, err := blobMgr.HashBlobFile(b.ContentHash)
		if err != nil {
			resp.Missing++
			store.MarkBlobValidated(ctx, b.ContentHash, false)
			continue
		}
		if actualHash == b.ContentHash {
			resp.Valid++
			store.MarkBlobValidated(ctx, b.ContentHash, true)
		} else {
			resp.Invalid++
			store.MarkBlobValidated(ctx, b.ContentHash, false)
		}
	}

	return resp, nil
}
