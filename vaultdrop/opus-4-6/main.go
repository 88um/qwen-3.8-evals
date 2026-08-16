package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"path/filepath"
)

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintf(os.Stderr, "usage: vaultdrop <serve|migrate>\n")
		os.Exit(1)
	}

	switch os.Args[1] {
	case "serve":
		runServe()
	case "migrate":
		runMigrate()
	default:
		fmt.Fprintf(os.Stderr, "unknown command: %s\n", os.Args[1])
		os.Exit(1)
	}
}

func stateDir() string {
	d := os.Getenv("VAULTDROP_STATE_DIR")
	if d == "" {
		d = "."
	}
	return d
}

func runMigrate() {
	dir := stateDir()
	if err := os.MkdirAll(dir, 0755); err != nil {
		log.Fatalf("create state dir: %v", err)
	}

	dbPath := filepath.Join(dir, "vaultdrop.db")
	store, err := NewStore(dbPath)
	if err != nil {
		log.Fatalf("open db: %v", err)
	}
	defer store.Close()

	if err := store.InitSchema(context.Background()); err != nil {
		log.Fatalf("init schema: %v", err)
	}

	log.Println("migration complete")
}

func runServe() {
	dir := stateDir()
	if err := os.MkdirAll(dir, 0755); err != nil {
		log.Fatalf("create state dir: %v", err)
	}

	// Load tenants config
	tenantsPath := filepath.Join(dir, "tenants.json")
	tenantsData, err := os.ReadFile(tenantsPath)
	if err != nil {
		log.Fatalf("read tenants.json: %v", err)
	}
	var cfg TenantsConfig
	if err := json.Unmarshal(tenantsData, &cfg); err != nil {
		log.Fatalf("parse tenants.json: %v", err)
	}

	tenantMap := make(map[string]string) // token → tenantID
	for _, t := range cfg.Tenants {
		tenantMap[t.Token] = t.ID
	}

	// Open database
	dbPath := filepath.Join(dir, "vaultdrop.db")
	store, err := NewStore(dbPath)
	if err != nil {
		log.Fatalf("open db: %v", err)
	}
	defer store.Close()

	ctx := context.Background()
	if err := store.InitSchema(ctx); err != nil {
		log.Fatalf("init schema: %v", err)
	}

	// Crash recovery: reset any uploads stuck in 'finalizing' state
	if err := store.RecoverCrash(ctx); err != nil {
		log.Fatalf("crash recovery: %v", err)
	}

	// Init blob storage directories
	blobMgr := NewBlobManager(dir)
	if err := blobMgr.Init(); err != nil {
		log.Fatalf("init blob dirs: %v", err)
	}

	handler := NewHandler(store, blobMgr, tenantMap, cfg.AdminToken)
	router := NewRouter(handler)

	port := os.Getenv("PORT")
	if port == "" {
		port = "8080"
	}

	addr := ":" + port
	log.Printf("vaultdrop listening on %s (state: %s)", addr, dir)
	if err := http.ListenAndServe(addr, router); err != nil {
		log.Fatalf("serve: %v", err)
	}
}
