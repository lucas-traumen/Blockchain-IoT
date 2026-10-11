#!/usr/bin/env node
// One-shot migration from the JSON stores to the SQLite stores.
//   node scripts/migrate_json_to_sqlite.js [--customers <file>] [--sessions <file>] [--db <file>] [--session-db <file>]
const path = require("node:path");
const { migrateJsonToSqlite } = require("../blockchain_api/sqliteStores");

function readFlag(name, fallback) {
    const index = process.argv.indexOf(`--${name}`);
    return index === -1 || index === process.argv.length - 1 ? fallback : process.argv[index + 1];
}

const dataDir = path.join(__dirname, "..", "blockchain_api", "data");
try {
    const report = migrateJsonToSqlite({
        customerJsonPath: readFlag("customers", process.env.HRC_CUSTOMER_STORE_PATH || path.join(dataDir, "customers.json")),
        sessionJsonPath: readFlag("sessions", process.env.HRC_DEMO_SESSION_STORE_PATH || path.join(dataDir, "demo-sessions.json")),
        dbPath: readFlag("db", process.env.HRC_APP_DB_PATH || path.join(dataDir, "customers.db")),
        sessionDbPath: readFlag("session-db", process.env.HRC_APP_SESSION_DB_PATH || path.join(dataDir, "demo-sessions.db"))
    });
    console.log(JSON.stringify(report, null, 2));
} catch (error) {
    console.error("Migration failed: " + error.message);
    process.exitCode = 1;
}
