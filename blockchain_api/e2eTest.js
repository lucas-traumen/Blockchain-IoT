// LIVE test: explicitly running this script submits real transactions. npm test is offline.
const RUN_ID = Date.now();
const API_URL =
    "http://localhost:3000";


// ======================================================
// HELPER
// ======================================================

function sleep(ms) {
    return new Promise(
        resolve =>
            setTimeout(resolve, ms)
    );
}


async function sendSafetyEvent(payload) {
    const token = process.env.HRC_INGEST_API_TOKEN;
    if (!token || token.length < 32) throw new Error("Set HRC_INGEST_API_TOKEN in the shell before the live E2E test");
    for (let attempt = 0; attempt < 20; attempt++) {
        const response = await fetch(`${API_URL}/api/safety-event`, {
            method: "POST",
            headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
            body: JSON.stringify(payload),
            signal: AbortSignal.timeout(25000)
        });
        const result = await response.json();
        if (!response.ok || !result.success) throw new Error(result.error || "API request failed");
        if (response.status !== 202) return result;
        await sleep(1000); // Same payload and identity: never create a replacement event.
    }
    throw new Error("Transaction still pending; retry the original request, not a new event");
}

async function getRobotStatus(
    deviceId
) {

    const response =
        await fetch(
            `${API_URL}/api/robots/${encodeURIComponent(deviceId)}/status`
        );


    const result =
        await response.json();


    if (!response.ok) {

        throw new Error(
            result.error
            || "Cannot get robot status"
        );
    }


    return result;
}


async function getEvent(
    eventId
) {

    const response =
        await fetch(
            `${API_URL}/api/events/${encodeURIComponent(eventId)}`
        );


    const result =
        await response.json();


    if (!response.ok) {

        throw new Error(
            result.error
            || "Cannot get event"
        );
    }


    return result;
}


// ======================================================
// TEST ONE RISK LEVEL
// ======================================================

async function testRiskLevel(
    riskLevel,
    deviceId,
    sequence
) {

    console.log(
        "\n========================================"
    );

    console.log(
        `TEST RISK LEVEL ${riskLevel}`
    );

    console.log(
        "========================================"
    );


    const timestamp =
        Math.floor(
            Date.now() / 1000
        );


    const sessionId =
        `E2E-SESSION-${RUN_ID}`;


    const anomaly =
        riskLevel >= 2;


    const predictionMap = {
        0: 0.05,
        1: 0.35,
        2: 0.72,
        3: 0.98
    };


    const sensorDataMap = {

        0: {
            distance: 1.5,
            speed: 0.5,
            force: 2.0
        },

        1: {
            distance: 0.9,
            speed: 0.8,
            force: 4.0
        },

        2: {
            distance: 0.45,
            speed: 1.3,
            force: 8.0
        },

        3: {
            distance: 0.12,
            speed: 2.8,
            force: 21.0
        }
    };


    const payload = {

        deviceId,

        timestamp,

        sessionId,

        sequence,

        sensorData:
            sensorDataMap[
                riskLevel
            ],

        aiResult: {

            anomaly,

            riskLevel,

            prediction:
                predictionMap[
                    riskLevel
                ]
        }
    };


    console.log(
        "Sending payload:"
    );

    console.log(
        JSON.stringify(
            payload,
            null,
            2
        )
    );


    const result =
        await sendSafetyEvent(
            payload
        );


    console.log(
        "\nBlockchain result:"
    );

    console.log(
        result
    );


    // --------------------------------------------------
    // VERIFY EVENT EXISTS
    // --------------------------------------------------

    const eventResult =
        await getEvent(
            result.eventId
        );


    console.log(
        "\nEvent read back:"
    );

    console.log(
        eventResult
    );


    // --------------------------------------------------
    // VERIFY ROBOT STATUS
    // --------------------------------------------------

    const robotStatus =
        await getRobotStatus(
            deviceId
        );


    console.log(
        "\nRobot status:"
    );

    console.log(
        robotStatus
    );


    // --------------------------------------------------
    // EXPECTED RESULT
    // --------------------------------------------------

    const shouldLock =
        riskLevel === 3;


    const actualLocked =
        robotStatus
            .robotLocked;


    const pass =
        shouldLock
        ===
        actualLocked;


    console.log(
        "\nExpected robot locked:",
        shouldLock
    );

    console.log(
        "Actual robot locked:",
        actualLocked
    );


    if (!pass) {

        throw new Error(
            `Risk level ${riskLevel} test failed`
        );
    }


    if (
        Number(
            eventResult
                .event
                .riskLevel
        )
        !== riskLevel
    ) {

        throw new Error(
            `Stored risk level mismatch for ${deviceId}`
        );
    }


    console.log(
        `\nPASS: Risk Level ${riskLevel}`
    );


    return {

        riskLevel,

        deviceId,

        eventId:
            result.eventId,

        dataHash:
            result.dataHash,

        transactionHash:
            result
                .recordTransactionHash,

        blockNumber:
            result
                .recordBlockNumber,

        emergencyStopTriggered:
            result
                .emergencyStopTriggered,

        robotLocked:
            result
                .robotLocked
    };
}


// ======================================================
// MAIN E2E TEST
// ======================================================

async function main() {

    console.log(
        "\n========================================"
    );

    console.log(
        "HRC SAFETY LOG - END TO END TEST"
    );

    console.log(
        "========================================"
    );


    try {

        const results = [];


        // SAFE
        results.push(
            await testRiskLevel(
                0,
                "E2E-ROBOT-SAFE",
                1
            )
        );


        await sleep(1100);


        // LOW
        results.push(
            await testRiskLevel(
                1,
                "E2E-ROBOT-LOW",
                2
            )
        );


        await sleep(1100);


        // WARNING
        results.push(
            await testRiskLevel(
                2,
                "E2E-ROBOT-WARNING",
                3
            )
        );


        await sleep(1100);


        // DANGER
        results.push(
            await testRiskLevel(
                3,
                "E2E-ROBOT-DANGER",
                4
            )
        );


        console.log(
            "\n========================================"
        );

        console.log(
            "FINAL E2E TEST SUMMARY"
        );

        console.log(
            "========================================"
        );


        for (
            const result
            of results
        ) {

            console.log(
                `Risk ${result.riskLevel}`
                +
                ` | ${result.deviceId}`
                +
                ` | Locked: ${result.robotLocked}`
                +
                ` | E-Stop: ${result.emergencyStopTriggered}`
                +
                ` | Block: ${result.blockNumber}`
            );
        }


        console.log(
            "\nALL END-TO-END TESTS PASSED"
        );


    } catch (error) {

        console.error(
            "\nEND-TO-END TEST FAILED"
        );

        console.error(
            error.message
        );

        process.exitCode =
            1;
    }
}


main();