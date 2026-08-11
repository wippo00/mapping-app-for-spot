(function () {
    const initialFallbackPosition = [43.7696, 11.2558];

    const elements = {
        message: document.getElementById("message"),
        messageText: document.getElementById("messageText"),
        messageIcon: document.getElementById("messageIcon"),
        missionName: document.getElementById("missionName"),
        cellSize: document.getElementById("cellSize"),
        cellSizeValue: document.getElementById("cellSizeValue"),
        gridRotation: document.getElementById("gridRotation"),
        gridRotationValue: document.getElementById("gridRotationValue"),
        positionGpsButton: document.getElementById("positionGpsButton"),
        positionManualButton: document.getElementById("positionManualButton"),
        positionModeStatus: document.getElementById("positionModeStatus"),
        sendMissionButton: document.getElementById("sendMissionButton"),
        clearAreaButton: document.getElementById("clearAreaButton"),
        historyButton: document.getElementById("historyButton"),
        abortButton: document.getElementById("abortButton"),
        spotMessagesStatus: document.getElementById("spotMessagesStatus"),
        spotMessagesList: document.getElementById("spotMessagesList"),
        mobileDockTop: document.getElementById("mobileDockTop"),
        dockMessagesButton: document.getElementById("dockMessagesButton"),
        dockActionButton: document.getElementById("dockActionButton"),
        dockHistoryButton: document.getElementById("dockHistoryButton")
    };

    if (typeof L === "undefined") {
        setMessage(
            "Leaflet non è stato caricato. Verifica la connessione o rendi la libreria disponibile localmente.",
            "error"
        );
        return;
    }

    let robotPosition = initialFallbackPosition.slice();
    let selectedPoints = [];
    let pointMarkers = [];
    let selectedArea = null;
    let gridLayer = null;
    let currentGridCellCount = 0;
    const currentUserId = document.body.dataset.userId || "guest";
    const currentMissionStorageKey = `currentMissionId:${currentUserId}`;
    const robotAccuracyStorageKey = `robotGpsAccuracy:${currentUserId}`;
    const robotPositionModeStorageKey = `robotPositionMode:${currentUserId}`;
    const manualRobotPositionStorageKey = `manualRobotPosition:${currentUserId}`;
    const storedRobotAccuracy = Number(localStorage.getItem(robotAccuracyStorageKey));
    const storedPositionMode =
        localStorage.getItem(robotPositionModeStorageKey) === "manual"
            ? "manual"
            : "gps";
    const storedManualRobotPosition = (function () {
        try {
            return JSON.parse(
                localStorage.getItem(manualRobotPositionStorageKey) || "null"
            );
        } catch (error) {
            localStorage.removeItem(manualRobotPositionStorageKey);
            return null;
        }
    })();
    let currentRobotAccuracy =
        Number.isFinite(storedRobotAccuracy) && storedRobotAccuracy > 0
            ? storedRobotAccuracy
            : null;
    let robotPositionMode = storedPositionMode;
    let manualRobotSelectionArmed = false;
    let latestGpsPosition = null;
    let latestGpsAccuracy = null;

    if (
        robotPositionMode === "manual" &&
        Array.isArray(storedManualRobotPosition) &&
        storedManualRobotPosition.length === 2 &&
        Number.isFinite(Number(storedManualRobotPosition[0])) &&
        Number.isFinite(Number(storedManualRobotPosition[1]))
    ) {
        robotPosition = [
            Number(storedManualRobotPosition[0]),
            Number(storedManualRobotPosition[1])
        ];
        currentRobotAccuracy = null;
    }

    let currentMissionId = null;
    let messageSimulationTimer = null;
    let activeMessageMissionId = null;

    window.currentMissionId = currentMissionId;

    const map = L.map("map", {
        maxZoom: 22
    }).setView(robotPosition, 16);

    const streetLayer = L.tileLayer(
        "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
        {
            maxZoom: 22,
            maxNativeZoom: 19,
            attribution: "&copy; OpenStreetMap contributors"
        }
    );

    const satelliteLayer = L.tileLayer(
        "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        {
            maxZoom: 22,
            maxNativeZoom: 19,
            attribution: "Tiles &copy; Esri"
        }
    );

    streetLayer.addTo(map);

    L.control.layers(
        {
            "Mappa stradale": streetLayer,
            "Satellite": satelliteLayer
        },
        null,
        {
            position: "topright"
        }
    ).addTo(map);

    const robotAccuracyCircle = L.circle(robotPosition, {
        radius: currentRobotAccuracy || 1,
        color: "#1a73e8",
        weight: 2,
        opacity: currentRobotAccuracy ? 0.55 : 0,
        fillColor: "#1a73e8",
        fillOpacity: currentRobotAccuracy ? 0.14 : 0,
        interactive: false
    }).addTo(map);

    const spotMarker = L.circleMarker(robotPosition, {
        radius: 8,
        color: "#ffffff",
        weight: 3,
        opacity: 1,
        fillColor: "#1a73e8",
        fillOpacity: 1
    })
        .addTo(map)
        .bindPopup("Posizione iniziale in attesa del GPS");

    function updateRobotPositionMarker(position, accuracyMeters, popupText) {
        const latitude = Number(position[0]);
        const longitude = Number(position[1]);

        if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) {
            return;
        }

        robotPosition = [latitude, longitude];

        const latLng = L.latLng(latitude, longitude);
        robotAccuracyCircle.setLatLng(latLng);
        spotMarker.setLatLng(latLng);

        const numericAccuracy = Number(accuracyMeters);

        if (numericAccuracy === 0) {
            currentRobotAccuracy = null;
            localStorage.removeItem(robotAccuracyStorageKey);
        } else if (Number.isFinite(numericAccuracy) && numericAccuracy > 0) {
            currentRobotAccuracy = numericAccuracy;
            localStorage.setItem(
                robotAccuracyStorageKey,
                String(currentRobotAccuracy)
            );
        }

        if (currentRobotAccuracy) {
            robotAccuracyCircle.setRadius(currentRobotAccuracy);
            robotAccuracyCircle.setStyle({
                opacity: 0.55,
                fillOpacity: 0.14
            });
        } else {
            robotAccuracyCircle.setStyle({
                opacity: 0,
                fillOpacity: 0
            });
        }

        if (popupText) {
            spotMarker.bindPopup(popupText);
        }

        spotMarker.bringToFront();
    }

    function renderPositionModeControls() {
        const gpsActive = robotPositionMode === "gps";
        elements.positionGpsButton.classList.toggle("active", gpsActive);
        elements.positionManualButton.classList.toggle("active", !gpsActive);

        if (gpsActive) {
            elements.positionModeStatus.textContent =
                "Posizione aggiornata dal GPS.";
        } else if (manualRobotSelectionArmed) {
            elements.positionModeStatus.textContent =
                "Tocca la mappa per impostare il punto di partenza.";
        } else {
            elements.positionModeStatus.textContent =
                "Posizione impostata manualmente.";
        }
    }

    function setRobotPositionMode(mode) {
        robotPositionMode = mode === "manual" ? "manual" : "gps";
        localStorage.setItem(robotPositionModeStorageKey, robotPositionMode);
        manualRobotSelectionArmed = robotPositionMode === "manual";

        if (robotPositionMode === "gps") {
            localStorage.removeItem(manualRobotPositionStorageKey);
            setMessage("Aggiornamento posizione GPS in corso...");

            if (latestGpsPosition) {
                updateRobotPositionMarker(
                    latestGpsPosition,
                    latestGpsAccuracy,
                    latestGpsAccuracy
                        ? `GPS rilevato · precisione ${latestGpsAccuracy.toFixed(0)} m`
                        : "GPS rilevato"
                );

                if (selectedPoints.length === 0) {
                    map.setView(robotPosition, 19);
                }

                setMessage(
                    latestGpsAccuracy
                        ? `GPS rilevato. Precisione stimata: ${latestGpsAccuracy.toFixed(0)} m.`
                        : "GPS rilevato."
                );
            }

            if (navigator.geolocation) {
                navigator.geolocation.getCurrentPosition(
                    handleGpsPosition,
                    handleGpsError,
                    {
                        enableHighAccuracy: true,
                        timeout: 15000,
                        maximumAge: 0
                    }
                );
            }
        } else {
            setMessage("Tocca la mappa per impostare la posizione di partenza.");

            if (window.matchMedia("(max-width: 820px)").matches) {
                closeMobileParameters();
            }
        }

        renderPositionModeControls();
    }

    function setManualRobotPosition(point) {
        updateRobotPositionMarker(
            point,
            0,
            "Posizione impostata manualmente"
        );
        localStorage.setItem(
            manualRobotPositionStorageKey,
            JSON.stringify(robotPosition)
        );
        manualRobotSelectionArmed = false;
        renderPositionModeControls();
        setMessage("Posizione di partenza impostata manualmente.");
    }

    function setMessage(text, type) {
        elements.messageText.textContent = text;
        elements.message.classList.remove("warning", "error");
        elements.messageIcon.textContent = type === "error" ? "!" : "i";

        if (type) {
            elements.message.classList.add(type);
        }
    }

    function escapeHtml(value) {
        return String(value)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    function formatDuration(seconds) {
        if (!seconds || seconds < 60) {
            return `${seconds || 0} s`;
        }

        const minutes = Math.floor(seconds / 60);
        const remainingSeconds = seconds % 60;

        if (remainingSeconds === 0) {
            return `${minutes} min`;
        }

        return `${minutes} min ${remainingSeconds} s`;
    }

    async function fetchMissionProgress(missionId) {
        const response = await fetch(`/missions/${missionId}/progress`);

        if (!response.ok) {
            return null;
        }

        return response.json();
    }

    async function fetchMissionDetail(missionId) {
        const response = await fetch(`/missions/${missionId}`);

        if (!response.ok) {
            return null;
        }

        return response.json();
    }

    async function fetchActiveMission() {
        const response = await fetch("/missions/active");

        if (!response.ok) {
            return null;
        }

        return response.json();
    }

    function formatProgressSummary(progress) {
        if (!progress) {
            return "Progresso non disponibile";
        }

        if (progress.status !== "INVIATA") {
            return progress.status_label;
        }

        return `Tempo restante stimato: ${formatDuration(progress.estimated_remaining_seconds)}`;
    }

    function missionIsActive() {
        return Boolean(getActiveMissionId());
    }

    function getStoredMissionId() {
        return Number(localStorage.getItem(currentMissionStorageKey)) || null;
    }

    function getActiveMissionId() {
        return currentMissionId || activeMessageMissionId;
    }

    function missionCanBeAborted(status) {
        return status === "INVIATA";
    }

    function setCurrentMissionId(missionId) {
        currentMissionId = missionId ? Number(missionId) : null;
        window.currentMissionId = currentMissionId;

        if (currentMissionId) {
            localStorage.setItem(currentMissionStorageKey, String(currentMissionId));
        } else {
            localStorage.removeItem(currentMissionStorageKey);
        }

        updateMissionControls();
        renderMobileDock();
    }

    function toLocalMeters(lat, lng, originLat, originLng) {
        const metersPerDegreeLat = 111320;
        const metersPerDegreeLng =
            111320 * Math.cos(originLat * Math.PI / 180);

        return {
            x: (lng - originLng) * metersPerDegreeLng,
            y: (lat - originLat) * metersPerDegreeLat
        };
    }

    function toLatLng(x, y, originLat, originLng) {
        const metersPerDegreeLat = 111320;
        const metersPerDegreeLng =
            111320 * Math.cos(originLat * Math.PI / 180);

        return {
            lat: originLat + y / metersPerDegreeLat,
            lng: originLng + x / metersPerDegreeLng
        };
    }

    function calculatePolygonArea(polygon) {
        let area = 0;

        for (let i = 0; i < polygon.length; i++) {
            const next = (i + 1) % polygon.length;

            area +=
                polygon[i].x * polygon[next].y -
                polygon[next].x * polygon[i].y;
        }

        return Math.abs(area) / 2;
    }

    function getAreaInSquareMeters() {
        if (selectedPoints.length < 3) {
            return 0;
        }

        const originLat = selectedPoints[0][0];
        const originLng = selectedPoints[0][1];

        const polygonMeters = selectedPoints.map(function (point) {
            return toLocalMeters(point[0], point[1], originLat, originLng);
        });

        return calculatePolygonArea(polygonMeters);
    }

    function pointInsidePolygon(point, polygon) {
        let inside = false;

        for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
            const xi = polygon[i].x;
            const yi = polygon[i].y;
            const xj = polygon[j].x;
            const yj = polygon[j].y;

            const intersects =
                ((yi > point.y) !== (yj > point.y)) &&
                point.x < ((xj - xi) * (point.y - yi)) / (yj - yi) + xi;

            if (intersects) {
                inside = !inside;
            }
        }

        return inside;
    }

    function rotatePoint(point, angleRadians) {
        return {
            x: point.x * Math.cos(angleRadians) - point.y * Math.sin(angleRadians),
            y: point.x * Math.sin(angleRadians) + point.y * Math.cos(angleRadians)
        };
    }

    function clearGrid() {
        if (gridLayer) {
            map.removeLayer(gridLayer);
            gridLayer = null;
        }

        currentGridCellCount = 0;
    }

    function setPlanningMessage(type) {
        const pointText = selectedPoints.length === 1
            ? "1 punto"
            : `${selectedPoints.length} punti`;
        const cellSize = parseFloat(elements.cellSize.value);
        const rotationDegrees = parseFloat(elements.gridRotation.value);

        if (selectedPoints.length < 3) {
            setMessage(
                `${pointText} selezionati · ` +
                `servono 3 punti · celle ${cellSize} m · rotazione ${rotationDegrees}°`,
                type
            );
            return;
        }

        const area = getAreaInSquareMeters();
        const gridText = currentGridCellCount > 0
            ? `${currentGridCellCount} celle`
            : "griglia da aggiornare";

        setMessage(
            `Area ${area.toFixed(1)} m² · ` +
            `${gridText} · celle ${cellSize} m · rotazione ${rotationDegrees}°`,
            type
        );
    }

    function redrawArea() {
        if (selectedArea) {
            map.removeLayer(selectedArea);
            selectedArea = null;
        }

        if (selectedPoints.length < 3) {
            clearGrid();
            setPlanningMessage();
            return;
        }

        selectedArea = L.polygon(selectedPoints, {
            color: "green",
            fillColor: "green",
            fillOpacity: 0.20
        }).addTo(map);

        generateGrid();
    }

    function generateGrid() {
        if (selectedPoints.length < 3) {
            return;
        }

        clearGrid();

        const cellSize = parseFloat(elements.cellSize.value);
        const rotationDegrees = parseFloat(elements.gridRotation.value);
        const rotationRadians = rotationDegrees * Math.PI / 180;

        const originLat = selectedPoints[0][0];
        const originLng = selectedPoints[0][1];

        const polygonMeters = selectedPoints.map(function (point) {
            return toLocalMeters(point[0], point[1], originLat, originLng);
        });

        const originalXs = polygonMeters.map(point => point.x);
        const originalYs = polygonMeters.map(point => point.y);

        const pivot = {
            x: (Math.min(...originalXs) + Math.max(...originalXs)) / 2,
            y: (Math.min(...originalYs) + Math.max(...originalYs)) / 2
        };

        const rotatedPolygon = polygonMeters.map(function (point) {
            return rotatePoint(
                {
                    x: point.x - pivot.x,
                    y: point.y - pivot.y
                },
                -rotationRadians
            );
        });

        const xs = rotatedPolygon.map(point => point.x);
        const ys = rotatedPolygon.map(point => point.y);

        const minX = Math.min(...xs);
        const maxX = Math.max(...xs);
        const minY = Math.min(...ys);
        const maxY = Math.max(...ys);

        const estimatedCells =
            Math.ceil((maxX - minX) / cellSize) *
            Math.ceil((maxY - minY) / cellSize);

        const area = getAreaInSquareMeters();

        if (estimatedCells > 3000) {
            currentGridCellCount = 0;
            setMessage(
                `Area ${area.toFixed(1)} m² · troppo grande per celle ${cellSize} m · ` +
                `rotazione ${rotationDegrees}°`,
                "warning"
            );
            return;
        }

        gridLayer = L.layerGroup().addTo(map);

        let validCells = 0;

        for (let x = minX; x < maxX; x += cellSize) {
            for (let y = minY; y < maxY; y += cellSize) {
                const localCorners = [
                    { x: x, y: y },
                    { x: x + cellSize, y: y },
                    { x: x + cellSize, y: y + cellSize },
                    { x: x, y: y + cellSize }
                ];

                const actualCorners = localCorners.map(function (corner) {
                    const rotatedBack = rotatePoint(corner, rotationRadians);

                    return {
                        x: rotatedBack.x + pivot.x,
                        y: rotatedBack.y + pivot.y
                    };
                });

                const localCenter = {
                    x: x + cellSize / 2,
                    y: y + cellSize / 2
                };

                const rotatedCenter = rotatePoint(localCenter, rotationRadians);

                const actualCenter = {
                    x: rotatedCenter.x + pivot.x,
                    y: rotatedCenter.y + pivot.y
                };

                const hasCornerInside = actualCorners.some(function (corner) {
                    return pointInsidePolygon(corner, polygonMeters);
                });

                const centerInside = pointInsidePolygon(actualCenter, polygonMeters);

                if (!hasCornerInside && !centerInside) {
                    continue;
                }

                const latLngCorners = actualCorners.map(function (corner) {
                    const point = toLatLng(
                        corner.x,
                        corner.y,
                        originLat,
                        originLng
                    );

                    return [point.lat, point.lng];
                });

                L.polygon(latLngCorners, {
                    color: "#1565c0",
                    weight: 1,
                    fillOpacity: 0.05
                }).addTo(gridLayer);

                validCells++;
            }
        }

        currentGridCellCount = validCells;
        setPlanningMessage();
    }

    function resetSelectedArea() {
        selectedPoints = [];

        pointMarkers.forEach(function (marker) {
            map.removeLayer(marker);
        });

        pointMarkers = [];

        if (selectedArea) {
            map.removeLayer(selectedArea);
            selectedArea = null;
        }

        clearGrid();
    }

    function clearSelectedArea() {
        if (missionIsActive()) {
            setMessage("La missione è attiva: abortisci o attendi il rientro prima di modificare l'area.", "warning");
            return;
        }

        resetSelectedArea();
        setMessage("Area e griglia cancellate.");

        if (window.matchMedia("(max-width: 820px)").matches) {
            closeMobileParameters();
        }
    }

    function updateSliderLabels() {
        elements.cellSizeValue.textContent = `${elements.cellSize.value} m`;
        elements.gridRotationValue.textContent = `${elements.gridRotation.value}°`;
    }

    function resetMissionParameters() {
        elements.missionName.value = "";
        elements.cellSize.value = "2";
        elements.gridRotation.value = "0";
        updateSliderLabels();
    }

    function updateMissionControls() {
        const activeMissionId = getActiveMissionId();
        const active = Boolean(activeMissionId);

        if (activeMissionId && currentMissionId !== activeMissionId) {
            currentMissionId = activeMissionId;
            window.currentMissionId = currentMissionId;
        }

        document.body.classList.toggle("mission-active", active);

        elements.cellSize.disabled = active;
        elements.missionName.disabled = active;
        elements.gridRotation.disabled = active;
        elements.clearAreaButton.disabled = active;
        elements.positionGpsButton.disabled = active;
        elements.positionManualButton.disabled = active;
        elements.sendMissionButton.disabled = false;
        elements.abortButton.disabled = true;

        elements.sendMissionButton.classList.toggle("btn-primary", !active);
        elements.sendMissionButton.classList.toggle("btn-danger", active);
        elements.sendMissionButton.dataset.state = active ? "abort" : "send";
        elements.sendMissionButton.textContent =
            active ? "Abort missione" : "Invia missione";
        renderPositionModeControls();
    }

    function syncMobileDockHeight() {
        const isMobile = window.matchMedia("(max-width: 820px)").matches;

        if (!isMobile) {
            document.documentElement.style.removeProperty("--mobile-dock-height");
            return;
        }

        const height = Math.ceil(
            document.getElementById("mobileDock").getBoundingClientRect().height
        );

        if (height > 0) {
            document.documentElement.style.setProperty(
                "--mobile-dock-height",
                `${height}px`
            );
        }
    }

    function openMobileParameters() {
        if (missionIsActive()) {
            return;
        }

        document.body.classList.add("mobile-params-open");
        setTimeout(function () {
            syncMobileDockHeight();
            map.invalidateSize();
            keepSelectedAreaVisible();
        }, 120);
        setTimeout(keepSelectedAreaVisible, 280);
        renderMobileDock();
    }

    function closeMobileParameters(shouldRender) {
        document.body.classList.remove("mobile-params-open");
        setTimeout(function () {
            syncMobileDockHeight();
            map.invalidateSize();
        }, 120);

        if (shouldRender !== false) {
            renderMobileDock();
        }
    }

    function keepSelectedAreaVisible() {
        if (selectedPoints.length < 3) {
            return;
        }

        const bounds = selectedArea
            ? selectedArea.getBounds()
            : L.latLngBounds(selectedPoints);
        const mapRect = map.getContainer().getBoundingClientRect();
        const missionPanel = document.querySelector(".mission-panel");
        const panelIsOpen = document.body.classList.contains("mobile-params-open");
        let bottomPadding = 12;

        if (panelIsOpen && missionPanel) {
            const panelRect = missionPanel.getBoundingClientRect();
            const coveredMapHeight = Math.max(
                0,
                mapRect.bottom - Math.max(mapRect.top, panelRect.top)
            );

            bottomPadding = Math.min(
                coveredMapHeight + 8,
                Math.max(18, mapRect.height - 56)
            );
        }

        map.fitBounds(bounds, {
            paddingTopLeft: [12, 12],
            paddingBottomRight: [
                12,
                bottomPadding
            ],
            maxZoom: 21,
            animate: true
        });
    }

    function renderMobileDock() {
        const active = missionIsActive();

        if (!active) {
            elements.dockActionButton.className = "start";
            elements.dockActionButton.textContent = "Inizia";

            const isOpen = document.body.classList.contains("mobile-params-open");
            elements.mobileDockTop.innerHTML = `
                <button id="mobileParametersButton" type="button">
                    ${isOpen ? "Chiudi parametri missione" : "Parametri missione"}
                </button>
            `;

            document
                .getElementById("mobileParametersButton")
                .addEventListener("click", function () {
                    if (isOpen) {
                        closeMobileParameters();
                    } else {
                        openMobileParameters();
                    }
                });

            syncMobileDockHeight();
            return;
        }

        closeMobileParameters(false);

        elements.dockActionButton.className = "abort";
        elements.dockActionButton.textContent = "Abort";

        renderLatestMessageInDock(currentMissionId);
        syncMobileDockHeight();
    }

    async function renderLatestMessageInDock(missionId) {
        if (missionId !== currentMissionId) {
            return;
        }

        try {
            const response = await fetch(`/missions/${missionId}/messages`);

            if (!response.ok) {
                return;
            }

            const messages = await response.json();
            const progress = await fetchMissionProgress(missionId);
            const titleProgress = progress && progress.status !== "INVIATA"
                ? progress.status_label.toUpperCase()
                : "IN CORSO";

            if (missionId !== currentMissionId) {
                return;
            }

            if (messages.length === 0) {
                elements.mobileDockTop.innerHTML = `
	                    <div class="dock-notification">
	                        <div class="dock-notification-title">
	                            MISSIONE #${missionId} · ${titleProgress}
	                        </div>
	                        <div class="dock-notification-text">
	                            ${escapeHtml(formatProgressSummary(progress))}
	                        </div>
	                    </div>
	                `;
                syncMobileDockHeight();
                return;
            }

            const latest = messages[0];
            const warningClass = latest.level === "WARNING" ? "warning" : "";

            elements.mobileDockTop.innerHTML = `
	                <div class="dock-notification ${warningClass}">
	                    <div class="dock-notification-title">
	                        MISSIONE #${missionId} · ${titleProgress} · ${escapeHtml(latest.level)}
	                    </div>
	                    <div class="dock-notification-text">
	                        ${escapeHtml(latest.text)}
                    </div>
                </div>
            `;
            syncMobileDockHeight();
        } catch (error) {
            console.error(error);
        }
    }

    function renderSpotMessages(messages) {
        elements.spotMessagesList.textContent = "";

        if (messages.length === 0) {
            const empty = document.createElement("p");
            empty.textContent = "Nessun messaggio ricevuto dalla missione.";
            elements.spotMessagesList.appendChild(empty);
            return;
        }

        messages.forEach(function (message) {
            const card = document.createElement("article");
            card.className =
                message.level === "WARNING"
                    ? "spot-message warning"
                    : "spot-message";

            const title = document.createElement("strong");
            title.textContent = message.level;

            const time = document.createElement("small");
            time.textContent = message.created_at.replace("T", " ");

            const text = document.createElement("div");
            text.textContent = message.text;

            card.appendChild(title);
            card.appendChild(time);
            card.appendChild(text);
            elements.spotMessagesList.appendChild(card);
        });
    }

    async function loadSpotMessages(missionId) {
        if (missionId !== currentMissionId) {
            return;
        }

        const response = await fetch(`/missions/${missionId}/messages`);

        if (!response.ok) {
            return;
        }

        const messages = await response.json();
        const progress = await fetchMissionProgress(missionId);

        if (missionId !== currentMissionId) {
            return;
        }

        elements.spotMessagesStatus.textContent = progress
            ? `Missione #${missionId}: ${formatProgressSummary(progress)}.`
            : `Missione #${missionId}: ricezione messaggi simulati attiva.`;
        renderSpotMessages(messages);
        renderMobileDock();
    }

    async function generateSimulatedSpotMessage(missionId) {
        if (missionId !== currentMissionId) {
            return;
        }

        const response = await fetch(
            `/missions/${missionId}/messages/simulate`,
            {
                method: "POST"
            }
        );

        if (!response.ok) {
            return;
        }

        const message = await response.json();

        if (missionId !== currentMissionId) {
            return;
        }

        if (message.status === "COMPLETATA") {
            await loadSpotMessages(missionId);

            if (missionId !== currentMissionId) {
                return;
            }

            resetMissionPlanningState(
                `Missione #${missionId}: mappatura completata. Configura una nuova missione.`
            );
            return;
        }

        await loadSpotMessages(missionId);
    }

    function startSpotMessageSimulation(missionId) {
        if (missionId && currentMissionId !== Number(missionId)) {
            currentMissionId = Number(missionId);
            window.currentMissionId = currentMissionId;
            localStorage.setItem(currentMissionStorageKey, String(currentMissionId));
        }

        updateMissionControls();

        if (activeMessageMissionId === missionId) {
            return;
        }

        if (messageSimulationTimer) {
            clearInterval(messageSimulationTimer);
        }

        activeMessageMissionId = missionId;
        elements.spotMessagesStatus.textContent =
            `Missione #${missionId}: ricezione messaggi simulati attiva.`;

        generateSimulatedSpotMessage(missionId);

        messageSimulationTimer = setInterval(function () {
            generateSimulatedSpotMessage(missionId);
        }, 5000);
    }

    function stopSpotMessageSimulation() {
        if (messageSimulationTimer) {
            clearInterval(messageSimulationTimer);
            messageSimulationTimer = null;
        }

        activeMessageMissionId = null;
        elements.spotMessagesStatus.textContent = "Nessuna missione attiva.";
        renderSpotMessages([]);
        updateMissionControls();
        renderMobileDock();
    }

    function resetMissionPlanningState(messageText, options) {
        const settings = options || {};
        const shouldOpenMobileParameters =
            settings.openMobileParameters !== false;

        stopSpotMessageSimulation();
        setCurrentMissionId(null);
        resetSelectedArea();
        resetMissionParameters();
        setMessage(messageText || "Configura una nuova missione.");

        if (
            window.matchMedia("(max-width: 820px)").matches &&
            shouldOpenMobileParameters
        ) {
            openMobileParameters();
        } else {
            closeMobileParameters();
        }
    }

    async function closeReturningMission(missionId, options) {
        try {
            const response = await fetch(
                `/missions/${missionId}/messages/return-complete`,
                {
                    method: "POST"
                }
            );

            if (!response.ok) {
                throw new Error("Errore durante la chiusura del rientro.");
            }
        } catch (error) {
            console.error(error);
        }

        resetMissionPlanningState(
            `Missione #${missionId}: rientro completato. Configura una nuova missione.`,
            options
        );
    }

    function restoreSavedMissionGeometry(mission) {
        if (
            !mission ||
            !Array.isArray(mission.polygon_vertices) ||
            mission.polygon_vertices.length < 3
        ) {
            return;
        }

        resetSelectedArea();

        elements.cellSize.value = String(Number(mission.cell_size_m));
        elements.missionName.value = mission.name || `Missione #${mission.id}`;
        elements.gridRotation.value = String(Number(mission.grid_rotation_deg));
        updateSliderLabels();

        if (
            Number.isFinite(Number(mission.robot_latitude)) &&
            Number.isFinite(Number(mission.robot_longitude))
        ) {
            updateRobotPositionMarker(
                [
                    Number(mission.robot_latitude),
                    Number(mission.robot_longitude)
                ],
                null,
                `Posizione iniziale missione #${mission.id}`
            );
        }

        selectedPoints = mission.polygon_vertices.map(function (vertex) {
            return [
                Number(vertex.latitude),
                Number(vertex.longitude)
            ];
        });

        redrawArea();

        const bounds = L.latLngBounds(selectedPoints);

        map.fitBounds(bounds, {
            padding: [28, 28],
            maxZoom: 21,
            animate: false
        });
    }

    async function restoreStoredMissionState() {
        const storedMissionId = getStoredMissionId();
        let mission = null;
        const keepParametersClosed = {
            openMobileParameters: false
        };

        try {
            if (storedMissionId) {
                mission = await fetchMissionDetail(storedMissionId);
            }

            if (
                !mission ||
                (
                    !missionCanBeAborted(mission.status) &&
                    mission.status !== "RIENTRO_BASE"
                )
            ) {
                mission = await fetchActiveMission();
            }

            if (!mission) {
                resetMissionPlanningState(
                    "Configura una nuova missione.",
                    keepParametersClosed
                );
                return;
            }

            const missionId = Number(mission.id);

            if (missionCanBeAborted(mission.status)) {
                restoreSavedMissionGeometry(mission);
                setCurrentMissionId(missionId);
                startSpotMessageSimulation(missionId);
                loadSpotMessages(missionId);
                return;
            }

            if (mission.status === "RIENTRO_BASE") {
                await closeReturningMission(
                    missionId,
                    keepParametersClosed
                );
                return;
            }

            resetMissionPlanningState(
                "Configura una nuova missione.",
                keepParametersClosed
            );
        } catch (error) {
            console.error(error);
            resetMissionPlanningState(
                "Configura una nuova missione.",
                keepParametersClosed
            );
        }
    }

    async function sendMission() {
        if (missionIsActive()) {
            setMessage("Esiste già una missione attiva.", "warning");
            return;
        }

        if (selectedPoints.length < 3) {
            setMessage("Errore: seleziona almeno 3 punti per creare una missione.", "error");
            return;
        }

        const cellSize = parseFloat(elements.cellSize.value);
        const missionName = elements.missionName.value.trim();

        if (!missionName) {
            setMessage("Errore: inserisci un nome missione.", "error");
            return;
        }

        const area = getAreaInSquareMeters();
        const payload = {
            name: missionName,
            robot_position: {
                latitude: robotPosition[0],
                longitude: robotPosition[1]
            },
            polygon_vertices: selectedPoints.map(function (point) {
                return {
                    latitude: point[0],
                    longitude: point[1]
                };
            }),
            cell_size_m: cellSize,
            grid_rotation_deg: parseFloat(elements.gridRotation.value),
            area_m2: area,
            grid_cell_count: currentGridCellCount ||
                Math.ceil(area / (cellSize * cellSize))
        };

        try {
            setMessage("Invio missione al database in corso...");

            const response = await fetch("/missions", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json"
                },
                body: JSON.stringify(payload)
            });

            if (!response.ok) {
                throw new Error("Errore durante il salvataggio.");
            }

            const result = await response.json();

            setCurrentMissionId(result.mission_id);
            closeMobileParameters();
            startSpotMessageSimulation(result.mission_id);

            setMessage(
                `${result.name || missionName} (#${result.mission_id}) avviata: ` +
                `${result.grid_cell_count} quadrati, durata stimata ` +
                `${formatDuration(result.estimated_duration_seconds)}.`
            );
        } catch (error) {
            setMessage("Errore: impossibile salvare la missione nel database.", "error");
            console.error(error);
        }
    }

    async function abortMission() {
        if (!currentMissionId) {
            setMessage("Nessuna missione attiva da abortire.", "warning");
            return;
        }

        const missionId = currentMissionId;
        const confirmed = confirm(`Vuoi abortire la missione #${missionId}?`);

        if (!confirmed) {
            return;
        }

        try {
            setMessage(`Abort della missione #${missionId} in corso...`, "warning");

            const response = await fetch(`/missions/${missionId}/abort`, {
                method: "POST"
            });

            if (!response.ok) {
                throw new Error("Errore durante l'abort.");
            }

            const result = await response.json();

            await fetch(`/missions/${result.mission_id}/messages/abort-start`, {
                method: "POST"
            });

            await loadSpotMessages(result.mission_id);

            setMessage(
                `Missione #${result.mission_id}: mission_active = false. ` +
                "Rientro obbligatorio al punto di partenza avviato.",
                "warning"
            );

            setTimeout(async function () {
                try {
                    const returnResponse = await fetch(
                        `/missions/${result.mission_id}/messages/return-complete`,
                        {
                            method: "POST"
                        }
                    );

                    if (!returnResponse.ok) {
                        throw new Error("Errore durante il rientro simulato.");
                    }

                    resetMissionPlanningState(
                        `Missione #${result.mission_id}: rientro completato. Configura una nuova missione.`
                    );
                } catch (error) {
                    setMessage("Errore durante il rientro simulato.", "error");
                    console.error(error);
                }
            }, 4000);
        } catch (error) {
            setMessage("Errore: impossibile abortire la missione.", "error");
            console.error(error);
        }
    }

    function handleGpsPosition(position) {
        const latitude = position.coords.latitude;
        const longitude = position.coords.longitude;
        const accuracy = position.coords.accuracy;
        latestGpsPosition = [latitude, longitude];
        latestGpsAccuracy = accuracy;

        if (robotPositionMode !== "gps") {
            return;
        }

        updateRobotPositionMarker(
            latestGpsPosition,
            accuracy,
            `GPS rilevato · precisione ${accuracy.toFixed(0)} m`
        );

        if (selectedPoints.length === 0) {
            map.setView(robotPosition, 19);
        }

        if (selectedPoints.length === 0 && !missionIsActive()) {
            setMessage(`GPS rilevato. Precisione stimata: ${accuracy.toFixed(0)} m.`);
        }
    }

    function handleGpsError() {
        if (
            robotPositionMode === "gps" &&
            selectedPoints.length === 0 &&
            !missionIsActive()
        ) {
            setMessage("GPS non disponibile: puoi comunque selezionare l'area.", "warning");
        }
    }

    elements.cellSize.addEventListener("input", function () {
        updateSliderLabels();

        if (selectedPoints.length >= 3) {
            generateGrid();
        } else if (selectedPoints.length > 0) {
            setPlanningMessage();
        }
    });

    elements.gridRotation.addEventListener("input", function () {
        updateSliderLabels();

        if (selectedPoints.length >= 3) {
            generateGrid();
        } else if (selectedPoints.length > 0) {
            setPlanningMessage();
        }
    });

    elements.positionGpsButton.addEventListener("click", function () {
        if (!missionIsActive()) {
            setRobotPositionMode("gps");
        }
    });

    elements.positionManualButton.addEventListener("click", function () {
        if (!missionIsActive()) {
            setRobotPositionMode("manual");
        }
    });

    elements.clearAreaButton.addEventListener("click", clearSelectedArea);
    elements.sendMissionButton.addEventListener("click", function () {
        if (missionIsActive()) {
            abortMission();
        } else {
            sendMission();
        }
    });
    elements.abortButton.addEventListener("click", abortMission);

    elements.historyButton.addEventListener("click", function () {
        window.location.href = "/history";
    });

    elements.dockMessagesButton.addEventListener("click", function () {
        window.location.href = "/messages";
    });

    elements.dockHistoryButton.addEventListener("click", function () {
        window.location.href = "/history";
    });

    elements.dockActionButton.addEventListener("click", function () {
        if (missionIsActive()) {
            abortMission();
        } else {
            closeMobileParameters();
            sendMission();
        }
    });

    map.on("click", function (event) {
        if (missionIsActive()) {
            setMessage("La missione è attiva: i parametri sono bloccati.", "warning");
            return;
        }

        const point = [event.latlng.lat, event.latlng.lng];

        if (robotPositionMode === "manual" && manualRobotSelectionArmed) {
            setManualRobotPosition(point);
            return;
        }

        selectedPoints.push(point);

        const marker = L.circleMarker(point, {
            radius: 6,
            color: "red",
            fillColor: "red",
            fillOpacity: 1
        }).addTo(map);

        pointMarkers.push(marker);
        redrawArea();
    });

    window.addEventListener("resize", function () {
        setTimeout(function () {
            syncMobileDockHeight();
            map.invalidateSize();
        }, 120);
    });

    if (navigator.geolocation) {
        navigator.geolocation.watchPosition(
            handleGpsPosition,
            handleGpsError,
            {
                enableHighAccuracy: true,
                timeout: 15000,
                maximumAge: 0
            }
        );
    } else {
        handleGpsError();
    }

    updateSliderLabels();
    syncMobileDockHeight();
    restoreStoredMissionState();
})();
