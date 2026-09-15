#include "Scenario.h"
#include "lib/utils.h"
#include "lib/rapidjson/writer.h"
#include "Rewarders\GeneralRewarder.h"
#include "Rewarders\LaneRewarder.h"
#include "Rewarders\SpeedRewarder.h"
#include "defaults.h"
#include <time.h>
#include <fstream>
#include <string>
#include <sstream>
#include "Functions.h"
#include "Constants.h"
#include <Eigen/Core>
#include <sstream>
#include "AreaRoaming.h"

// [longtail] Every spin-wait below was an unbounded `while (...) WAIT(0)`. A single
// bad vehicle model name -- or a path-node query that never settles -- hangs the
// SCRIPT THREAD forever. The failure is invisible from outside: the process stays
// alive, the window keeps responding, the control socket stays bound, and the
// client simply never receives another message again. Bound them, say which one
// gave up, and carry on rather than deadlocking the whole capture.
#define LT_WAIT_UNTIL(cond, ticks, what)                                        \
	do {                                                                        \
		int _lt_i = 0;                                                          \
		while (!(cond)) {                                                       \
			if (++_lt_i > (ticks)) {                                            \
				log(std::string("[longtail] TIMEOUT waiting for ") + (what), true); \
				break;                                                          \
			}                                                                   \
			WAIT(0);                                                            \
		}                                                                       \
	} while (0)


// #include "base64.h"

const int PEDESTRIAN_CLASS_ID = 10;

char* Scenario::weatherList[14] = { "CLEAR", "EXTRASUNNY", "CLOUDS", "OVERCAST", "RAIN", "CLEARING", "THUNDER", "SMOG", "FOGGY", "XMAS", "SNOWLIGHT", "BLIZZARD", "NEUTRAL", "SNOW" };
char* Scenario::vehicleList[3] = { "blista", "blista", "blista" };//voltic, packer

void Scenario::parseScenarioConfig(const Value& sc, bool setDefaults) {
	const Value& location = sc["location"];
	const Value& time = sc["time"];
	const Value& weather = sc["weather"];
	const Value& vehicle = sc["vehicle"];
	const Value& drivingMode = sc["drivingMode"];

	if (location.IsArray()) {
		if (!location[0].IsNull()) x = location[0].GetFloat();
		else if (setDefaults) x = 5000 * ((float)rand() / RAND_MAX) - 2500;

		if (!location[1].IsNull()) y = location[1].GetFloat(); 
		else if (setDefaults) y = 8000 * ((float)rand() / RAND_MAX) - 2000;

        if (!location[2].IsNull()) z = location[2].GetFloat();
        else if (setDefaults) z = 0;

        if (!location[3].IsNull()) {
            log("Location 2 is not null");
            startHeading = location[3].GetFloat();
        }
        else if (setDefaults) {
            log("Location 3 is NULL");
            startHeading = 0;
        }
	}
	else if (setDefaults) {
		x = 5000 * ((float)rand() / RAND_MAX) - 2500;
		y = 8000 * ((float)rand() / RAND_MAX) - 2000;
	}

	if (time.IsArray()) {
		if (!time[0].IsNull()) hour = time[0].GetInt();
		else if (setDefaults) hour = rand() % 24;

		if (!time[1].IsNull()) minute = time[1].GetInt();
		else if (setDefaults) minute = rand() % 60;
	}
	else if (setDefaults) {
        hour = 16;//TODO Do we want random times? rand() % 24;
		minute = rand() % 60;
	}

	if (!weather.IsNull()) _weather = weather.GetString();
    //TODO: Do we want other weather?
    else if (setDefaults) _weather = "CLEAR";// weatherList[rand() % 14];

	if (!vehicle.IsNull()) _vehicle = vehicle.GetString();
    else if (setDefaults) _vehicle = "ingot";// vehicleList[rand() % 3];

	if (drivingMode.IsArray()) {
		if (!drivingMode[0].IsNull()) _drivingMode = drivingMode[0].GetInt();
		// ⚠ Upstream wrote `rand() % 4294967296` here. MSVC's rand() returns
		// 0..32767, so the modulo is a no-op and the "random driving mode" was a
		// random draw from the low 15 bits -- an arbitrary mix of restraint flags
		// with no name. Default to NORMAL and let the client sample the style.
		else if (setDefaults)  _drivingMode = 786603;   // NORMAL
		// [longtail] upstream reads: if (drivingMode[1].IsNull()) _setSpeed = ...GetFloat();
		// The `!` is missing, so a client-supplied speed fell through to the
		// randomise branch (or was ignored entirely on Config, where
		// setDefaults is false) and GetFloat() was called on a null value.
		if (!drivingMode[1].IsNull()) _setSpeed = drivingMode[1].GetFloat();
		else if (setDefaults) _setSpeed = 1.0*(rand() % 10) + 10;
	}
	else if (setDefaults) {
		_drivingMode = -1;
	}
	if (!sc["spawnedEntitiesDespawnSeconds"].IsNull()) {
		spawnedEntitiesDespawnSeconds = sc["spawnedEntitiesDespawnSeconds"].GetDouble();
	}
	else if (setDefaults) {
		spawnedEntitiesDespawnSeconds = 60;
	}


}

void Scenario::parseDatasetConfig(const Value& dc, bool setDefaults) {
	if (!dc["rate"].IsNull()) rate = dc["rate"].GetFloat();
	else if (setDefaults) rate = _RATE_;

	if (!dc["frame"].IsNull()) {
		if (!dc["frame"][0].IsNull()) s_camParams.width = dc["frame"][0].GetInt();
		else if (setDefaults) s_camParams.width = _DEFAULT_CAMERA_WIDTH_;

		if (!dc["frame"][1].IsNull()) s_camParams.height = dc["frame"][1].GetInt();
		else if (setDefaults) s_camParams.height = _DEFAULT_CAMERA_HEIGHT_;
	}
	else if (setDefaults) {
		s_camParams.width = _DEFAULT_CAMERA_WIDTH_;
		s_camParams.height = _DEFAULT_CAMERA_HEIGHT_;
	}

    //Need to reset camera params when dataset config is received
    s_camParams.init = false;


    if (!dc["offscreen"].IsNull()) offscreen = dc["offscreen"].GetBool();
    else if (setDefaults) offscreen = _OFFSCREEN_;
    if (!dc["showBoxes"].IsNull()) showBoxes = dc["showBoxes"].GetBool();
    else if (setDefaults) showBoxes = _SHOWBOXES_;
    if (!dc["stationaryScene"].IsNull()) stationaryScene = dc["stationaryScene"].GetBool();
    else if (setDefaults) stationaryScene = _STATIONARY_SCENE_;
    if (!dc["collectTracking"].IsNull()) collectTracking = dc["collectTracking"].GetBool();
    else if (setDefaults) collectTracking = _COLLECT_TRACKING_;

	
    if (stationaryScene || TRUPERCEPT_SCENARIO) {
        vehiclesToCreate.clear();
        log("About to get vehicles");
        if (!dc["vehiclesToCreate"].IsNull()) {
            log("Vehicles non-null");
            const rapidjson::Value& jsonVehicles = dc["vehiclesToCreate"];
            for (rapidjson::SizeType i = 0; i < jsonVehicles.Size(); i++) {
                log("At least one");
                bool noHit = false;
                VehicleToCreate vehicleToCreate;
                const rapidjson::Value& jVeh = jsonVehicles[i];

                if (!jVeh[0].IsNull()) vehicleToCreate.model = jVeh[0].GetString();
                if (!jVeh[1].IsNull()) vehicleToCreate.forward = jVeh[1].GetFloat();
                if (!jVeh[2].IsNull()) vehicleToCreate.right = jVeh[2].GetFloat();
                if (!jVeh[3].IsNull()) vehicleToCreate.heading = jVeh[3].GetFloat();
                if (!jVeh[4].IsNull()) vehicleToCreate.color = jVeh[4].GetInt();
                if (!jVeh[5].IsNull()) vehicleToCreate.color2 = jVeh[5].GetInt();
                else noHit = true;

                if (!noHit) {
                    log("Pushing back vehicle");
                    vehiclesToCreate.push_back(vehicleToCreate);
                }
            }
        }
        pedsToCreate.clear();
        log("About to get ped");
        if (!dc["pedsToCreate"].IsNull()) {
            log("ped non-null");
            const rapidjson::Value& jsonPeds = dc["pedsToCreate"];
            for (rapidjson::SizeType i = 0; i < jsonPeds.Size(); i++) {
                log("At least one");
                bool noHit = false;
                PedToCreate pedToCreate;
                const rapidjson::Value& jPed = jsonPeds[i];

                if (!jPed[0].IsNull()) pedToCreate.model = jPed[0].GetInt();
                if (!jPed[1].IsNull()) pedToCreate.forward = jPed[1].GetFloat();
                if (!jPed[2].IsNull()) pedToCreate.right = jPed[2].GetFloat();
                if (!jPed[3].IsNull()) pedToCreate.heading = jPed[3].GetFloat();
                else noHit = true;

                if (!noHit) {
                    log("Pushing back ped");
                    pedsToCreate.push_back(pedToCreate);
                }
            }
        }
        vehicles_created = false;
    }


	exporter.parseDatasetConfig(dc, setDefaults);
	//exporter.camera = &camera;
	//exporter.cameraPositionOffset = &cameraPositionOffset;
	//exporter.cameraRotationOffset = &cameraRotationOffset;
	exporter.m_ownVehicle = &m_ownVehicle;
	//exporter.instance_index = &instance_index;
}

void Scenario::buildScenario() {
	Vector3 pos;
	Hash vehicleHash;
	float heading;

    if (!stationaryScene) {
        GAMEPLAY::SET_RANDOM_SEED(std::time(NULL));
        // [longtail] ⚠ Do NOT use PATHFIND::LOAD_ALL_PATH_NODES here. The 2017
        // pipeline used it, but that native (0x80E4A6EDDB0BE8D9) DOES NOT EXIST in
        // 1.0.3889.0 -- ScriptHookV aborts the game with
        //   "FATAL: Can't find native 0x80E4A6EDDB0BE8D9"
        // natives.h listing a hash proves nothing about the running game; natives
        // are removed between patches, and the archive targets 1.0.1103.
        LT_WAIT_UNTIL(PATHFIND::_0xF7B79A50B905A30D(-8192.0f, 8192.0f, -8192.0f, 8192.0f), 600, "path nodes to load");

        // The real defect was never node LOADING -- it was that the return value of
        // GET_CLOSEST_VEHICLE_NODE_WITH_HEADING was discarded. When it fails it
        // leaves `pos` unusable, and the ego gets placed on whatever geometry
        // happens to be there: a bridge railing, a rooftop, a ditch.
        BOOL gotNode = PATHFIND::GET_CLOSEST_VEHICLE_NODE_WITH_HEADING(x, y, 0, &pos, &heading, 1, 3.0f, 0);

        // ⚠ The heading this native reports is NOT reliably the direction of travel
        // along the road -- spawning with it puts the ego across the carriageway
        // (observed ~90 deg out on every clip, and the post-spawn drift check
        // reported ZERO error, proving the vehicle matched the node exactly and the
        // node itself was wrong). Derive the road direction from geometry instead:
        // the vector between two consecutive road nodes.
        if (gotNode) {
            Vector3 posB; float hB = 0.0f; Any unusedB = 0;
            if (PATHFIND::GET_NTH_CLOSEST_VEHICLE_NODE_WITH_HEADING(
                    pos.x, pos.y, pos.z, 2, &posB, &hB, &unusedB, 1, 3.0f, 2.5f)) {
                float dx = posB.x - pos.x, dy = posB.y - pos.y;
                if ((dx*dx + dy*dy) > 1.0f) {          // ignore coincident nodes
                    // GTA heading: 0 = +Y (north), increasing counter-clockwise.
                    float geo = (float)(atan2(-dx, dy) * 180.0 / PI);
                    while (geo < 0.0f) geo += 360.0f;
                    float d = geo - heading;
                    while (d > 180.0f) d -= 360.0f;
                    while (d < -180.0f) d += 360.0f;

                    // ⚠ The node-to-node vector gives the road AXIS, but its SENSE is
                    // arbitrary -- node 2 can lie behind node 1. Measured deltas of
                    // both 0 and 178.4 deg on consecutive spawns: taking the
                    // geometric heading raw points the ego the wrong way down the
                    // road half the time. Geometry supplies the axis; the node
                    // heading supplies the direction of travel. Snap to whichever
                    // end of the axis agrees with the node.
                    // ⚠ A node heading of exactly 0.0 is almost certainly UNSET --
                    // the native leaves the out-param untouched on some nodes
                    // (observed "node=0" alongside a geometric heading of 207 deg).
                    // Snapping to an unset value would align the ego to nothing.
                    // Being aligned to the road AXIS is what matters; which way
                    // along it is cosmetic on a two-way road, and "wrong way" is a
                    // scenario we deliberately generate anyway.
                    const bool nodeHeadingUsable = (heading > 0.01f || heading < -0.01f);
                    bool flipped = false;
                    if (nodeHeadingUsable && (d > 90.0f || d < -90.0f)) {
                        geo += 180.0f;
                        if (geo >= 360.0f) geo -= 360.0f;
                        d = (d > 0.0f) ? d - 180.0f : d + 180.0f;
                        flipped = true;
                    }
                    std::ostringstream hs;
                    hs << "[longtail] heading: node=" << heading
                       << (nodeHeadingUsable ? "" : " (UNSET)")
                       << " axis-aligned=" << geo
                       << " residual=" << d << (flipped ? " (flipped 180)" : "");
                    log(hs.str(), true);
                    heading = geo;
                }
            }
        }
        if (!gotNode) {
            // Walk outwards through the nth-closest nodes before giving up.
            for (int n = 2; n <= 12 && !gotNode; ++n) {
                Any unused = 0;
                gotNode = PATHFIND::GET_NTH_CLOSEST_VEHICLE_NODE_WITH_HEADING(
                              x, y, 0, n, &pos, &heading, &unused, 1, 3.0f, 2.5f);
            }
            std::ostringstream ns;
            ns << "[longtail] closest road node failed at (" << x << "," << y
               << "); nth-closest " << (gotNode ? "recovered" : "ALSO FAILED");
            log(ns.str(), true);
        }
    }

	log(std::string("[longtail] buildScenario: vehicle=") + _vehicle, true);
	ENTITY::DELETE_ENTITY(&m_ownVehicle);
	vehicleHash = GAMEPLAY::GET_HASH_KEY((char*)_vehicle);
	STREAMING::REQUEST_MODEL(vehicleHash);
	LT_WAIT_UNTIL(STREAMING::HAS_MODEL_LOADED(vehicleHash), 300, _vehicle);
	if (!STREAMING::HAS_MODEL_LOADED(vehicleHash)) {
		// An unrecognised model name never loads. Fall back so one bad entry in the
		// ego pool costs a clip, not the entire run.
		log(std::string("[longtail] ego model '") + _vehicle + "' never loaded; falling back to blista", true);
		vehicleHash = GAMEPLAY::GET_HASH_KEY("blista");
		STREAMING::REQUEST_MODEL(vehicleHash);
		LT_WAIT_UNTIL(STREAMING::HAS_MODEL_LOADED(vehicleHash), 300, "blista fallback");
	}
    if (stationaryScene) {
        pos.x = x;
        pos.y = y;
        pos.z = z;
        heading = startHeading;
        std::ostringstream oss;
        oss << "Start heading: " << startHeading;
        std::string str = oss.str();
        log(str);
        vehicles_created = false;
    }
	log("[longtail] bs: creating ego vehicle", true);
	// The 2017 pipeline retried until the entity existed; a single CREATE_VEHICLE
	// can fail (blocked position, model not resident yet) and then everything
	// downstream operates on a null vehicle.
	{
		int _tries = 0;
		m_ownVehicle = VEHICLE::CREATE_VEHICLE(vehicleHash, pos.x, pos.y, pos.z, heading, FALSE, FALSE);
		while (!ENTITY::DOES_ENTITY_EXIST(m_ownVehicle) && ++_tries < 120) {
			WAIT(0);
			m_ownVehicle = VEHICLE::CREATE_VEHICLE(vehicleHash, pos.x, pos.y, pos.z, heading, FALSE, FALSE);
		}
		if (!ENTITY::DOES_ENTITY_EXIST(m_ownVehicle)) {
			// ⚠ Previously this logged and carried on. Every subsequent native then
			// operated on a null entity -- grounding it, seating a ped in it,
			// attaching the camera -- and took the whole game down. If the ego
			// cannot be created there is no scenario; bail out and let the client
			// time out on this clip rather than crashing the process.
			log("[longtail] bs: CREATE_VEHICLE FAILED after retries -- ABORTING build", true);
			running = false;
			return;
		}
	}
	log("[longtail] bs: ego created, grounding", true);
	VEHICLE::SET_VEHICLE_ON_GROUND_PROPERLY(m_ownVehicle);
	// [longtail] CREATE_VEHICLE is given the road node's heading, but
	// SET_VEHICLE_ON_GROUND_PROPERLY re-settles the vehicle and can leave it
	// rotated -- which is how the ego ends up sitting ACROSS the carriageway
	// instead of pointing along it. Re-apply the node heading after grounding,
	// and again after the ped is seated, since the teleport can disturb it too.
	ENTITY::SET_ENTITY_HEADING(m_ownVehicle, heading);

	{
		int _i = 0;
		while (!ENTITY::DOES_ENTITY_EXIST(ped)) {
			ped = PLAYER::PLAYER_PED_ID();
			if (++_i > 600) { log("[longtail] TIMEOUT waiting for player ped", true); break; }
			WAIT(0);
		}
	}

	log("[longtail] bs: ped ok, teleporting", true);
	player = PLAYER::PLAYER_ID();
	PLAYER::START_PLAYER_TELEPORT(player, pos.x, pos.y, pos.z, heading, 0, 0, 0);
	LT_WAIT_UNTIL(!PLAYER::IS_PLAYER_TELEPORT_ACTIVE(), 600, "player teleport to finish");

	log("[longtail] bs: teleport done, seating ped", true);
	PED::SET_PED_INTO_VEHICLE(ped, m_ownVehicle, -1);
	ENTITY::SET_ENTITY_HEADING(m_ownVehicle, heading);
	VEHICLE::SET_VEHICLE_ON_GROUND_PROPERLY(m_ownVehicle);
	{
		float got = ENTITY::GET_ENTITY_HEADING(m_ownVehicle);
		float d = got - heading;
		while (d > 180.0f) d -= 360.0f;
		while (d < -180.0f) d += 360.0f;
		if (d > 15.0f || d < -15.0f) {
			std::ostringstream hs;
			hs << "[longtail] bs: heading off by " << d
			   << " deg (wanted " << heading << ", got " << got << ")";
			log(hs.str(), true);
		}
	}
	log("[longtail] bs: ped seated, releasing model", true);
	STREAMING::SET_MODEL_AS_NO_LONGER_NEEDED(vehicleHash);

	TIME::SET_CLOCK_TIME(hour, minute, 0);
	log("[longtail] bs: clock set", true);

	GAMEPLAY::SET_WEATHER_TYPE_NOW_PERSIST((char*)_weather);

	exporter.initialize();

    if (stationaryScene) {
        _setSpeed = 0;
    }

    CAM::RENDER_SCRIPT_CAMS(TRUE, FALSE, 0, TRUE, TRUE);

	// Vehicle handles are recycled by the engine, so a set carried across clips
	// would suppress re-tasking for a brand new vehicle that happens to reuse a
	// handle. Clear it with the scenario.
	m_retasked.clear();
	m_ignited.clear();
	++m_scenarioGen;
	exporter.setScenarioGen(m_scenarioGen);
	m_suppressAmbient = false;
	m_seedValue = 0;
	m_lastMovingClock = 0;
	m_unstuckIssued = false;
	m_vruTarget = 0;
	m_vruSpawnCursor = 0;
	AI::CLEAR_PED_TASKS(ped);
	if (_drivingMode >= 0 && !stationaryScene) {
		// egoDrivingModeOnRoad() masks IGNORE_ROADS / IGNORE_ALL_PATHING out of the
		// requested style: those release the wander task from the node network
		// entirely, and a 15 s clip of an embankment is not long-tail driving data.
		AI::TASK_VEHICLE_DRIVE_WANDER(ped, m_ownVehicle, _setSpeed, egoDrivingModeOnRoad());
    }

	// [longtail] Warmup elimination, part 1: do not wait for the ego to accelerate.
	// From a standing spawn, reaching ~17 m/s takes 5-8 s of wall clock that is
	// pure overhead on a 5 s clip. Setting the velocity directly removes it.
	if (!stationaryScene && _setSpeed > 0.0f) {
		VEHICLE::SET_VEHICLE_ENGINE_ON(m_ownVehicle, TRUE, TRUE, FALSE);
		VEHICLE::SET_VEHICLE_FORWARD_SPEED(m_ownVehicle, _setSpeed);
	}

	// [longtail] Warmup elimination, part 2: block until collision is actually
	// present. This one CANNOT be skipped -- without it the car falls through the
	// world. It is fast when prepareLocation() pre-warmed this spot during the
	// previous clip, which is the whole point of pipelining the request.
	STREAMING::REQUEST_COLLISION_AT_COORD(pos.x, pos.y, pos.z);
	STREAMING::LOAD_SCENE(pos.x, pos.y, pos.z);
	for (int i = 0; i < 600 && !ENTITY::HAS_COLLISION_LOADED_AROUND_ENTITY(m_ownVehicle); i++)
		WAIT(0);
	VEHICLE::SET_VEHICLE_ON_GROUND_PROPERLY(m_ownVehicle);
	// Ambient traffic fills faster with the low-priority generators on.
	VEHICLE::SET_ALL_LOW_PRIORITY_VEHICLE_GENERATORS_ACTIVE(TRUE);


}


// TODO Scenario::start and Scenario::config are very similar!
void Scenario::start(const Value& sc, const Value& dc) {
	if (running) return;

	running = true;
	//Parse options
	srand(std::time(NULL));
	parseScenarioConfig(sc, true);
	parseDatasetConfig(dc, true);

	//Build scenario
	buildScenario();

	running = true;
	lastSafetyCheck = std::clock();
}

void Scenario::config(const Value& sc, const Value& dc) {
	if (!running) return;

	//Parse options
	srand(std::time(NULL));
	parseScenarioConfig(sc, false);
	parseDatasetConfig(dc, false);

	//Build scenario
	buildScenario();

	lastSafetyCheck = std::clock();
}

void Scenario::run() {
	applyPendingControl();
	if (running && m_renderMode) { runRenderMode(); return; }
	if (running) {

		// [longtail] Hard invariants first, every frame, before anything else can
		// observe a broken state. Order matters: guards, then density (THIS_FRAME
		// natives), then camera/ego repair.
		enforceGameOverGuards();
		hideHudThisFrame();
		makeTrafficAggressive();
		makePedsInteractive();
		applyDensityThisFrame();
		enforceEgoIntegrity();
		enforceRoadLeash();
		igniteWrecks();
		enforceEgoMobility();
		topUpVRUs();

		std::clock_t now = std::clock();

        if (SAME_TIME_OF_DAY) {
            TIME::SET_CLOCK_TIME(hour, minute, 0);
        }

		if (_drivingMode < 0) {
			CONTROLS::_SET_CONTROL_NORMAL(27, 71, currentThrottle); //[0,1]
			CONTROLS::_SET_CONTROL_NORMAL(27, 72, currentBrake); //[0,1]
			CONTROLS::_SET_CONTROL_NORMAL(27, 59, currentSteering); //[-1,1]
		}
		
		float delay = ((float)(now - lastSafetyCheck)) / CLOCKS_PER_SEC;
		if (delay > 10) {
            //Need to delay first camera parameters being set so native functions return correct values
            if (!s_camParams.firstInit) {
                s_camParams.init = false;
                exporter.setCamParams();
                s_camParams.firstInit = true;
            }

			lastSafetyCheck = std::clock();
			// [longtail] The upstream guard block lived here and applied every flag
			// unconditionally. It is now driven by m_survival so the long-tail
			// events we want to capture are not suppressed. See applySurvivalSettings().
			applySurvivalSettings();
		}

		despawnSpawnedObjectsAfterTime();

	}
	scriptWait(0);
}

void Scenario::stop() {
	if (!running) return;
	running = false;
	m_renderMode = false;
	exporter.setRenderMode(false);
	exporter.setRenderTarget(0);
	CAM::DESTROY_ALL_CAMS(TRUE);
	CAM::RENDER_SCRIPT_CAMS(FALSE, TRUE, 500, FALSE, FALSE);
	AI::CLEAR_PED_TASKS(ped);
	setCommands(0.0, 0.0, 0.0);
}

void Scenario::setCommands(float throttle, float brake, float steering) {
	currentThrottle = throttle;
	currentBrake = brake;
	currentSteering = steering;
}

StringBuffer Scenario::generateMessage() {
	return exporter.generateMessage();
}



//TODO remove
void Scenario::setRecording_active(bool x) {
	exporter.setRecording_active(x);
}

void Scenario::goToLocation(float x, float y, float z, float speed) {
	Hash vehicleHash;
	vehicleHash = GAMEPLAY::GET_HASH_KEY((char*)_vehicle);

	AI::CLEAR_PED_TASKS(ped);
	AI::TASK_VEHICLE_DRIVE_TO_COORD(ped, m_ownVehicle, x, y, z, speed, Any(1.f), vehicleHash, _drivingMode, 2.f, true);
}

void Scenario::teleportToLocation(float x, float y, float z) {
	//Hash vehicleHash;
	//float heading;

	//ENTITY::DELETE_ENTITY(&m_ownVehicle);
	//vehicleHash = GAMEPLAY::GET_HASH_KEY((char*)_vehicle);
	//STREAMING::REQUEST_MODEL(vehicleHash);

	//m_ownVehicle = VEHICLE::CREATE_VEHICLE(vehicleHash, x, y, z, heading, FALSE, FALSE);
	////VEHICLE::SET_VEHICLE_ON_GROUND_PROPERLY(m_ownVehicle);

	//while (!ENTITY::DOES_ENTITY_EXIST(ped)) {
	//	ped = PLAYER::PLAYER_PED_ID();
	//	WAIT(0);
	//}

	//player = PLAYER::PLAYER_ID();


	//PLAYER::START_PLAYER_TELEPORT(player, x, y, z, heading, 0, 0, 0);
	//while (PLAYER::IS_PLAYER_TELEPORT_ACTIVE()) WAIT(0);

	//PED::SET_PED_INTO_VEHICLE(ped, m_ownVehicle, -1);
	//STREAMING::SET_MODEL_AS_NO_LONGER_NEEDED(vehicleHash);


	//ENTITY::SET_ENTITY_COORDS(m_ownVehicle, x, y, z, 0, 0, 1);
	ENTITY::SET_ENTITY_COORDS_NO_OFFSET(m_ownVehicle, x, y, z, 0, 0, 1);
	ENTITY::SET_ENTITY_HAS_GRAVITY(m_ownVehicle, false);

	bool isStrong = true;
	int forceFlags = 0u;
	if (isStrong) forceFlags |= (1u << 0);
	//Set first bit	
	//LAST BOOL HAS TO BE FALSE (SCRIPT STOPS RUNNING)
	bool isDirRel = false;
	bool isMassRel = false;
	ENTITY::APPLY_FORCE_TO_ENTITY_CENTER_OF_MASS(m_ownVehicle, forceFlags, 0, 0, 5000, FALSE, isDirRel, isMassRel, FALSE);
	//ENTITY::APPLY_FORCE_TO_ENTITY_CENTER_OF_MASS(m_ownVehicle, 0, 0, 0, 50, false, BOOL p6, BOOL p7, BOOL p8);
	//ENTITY::APPLY_FORCE_TO_ENTITY(Entity entity, int forceType, float x, float y, float z, float xRot, float yRot, float zRot, int p8, BOOL isRel, BOOL ignoreUpVec, BOOL p11, BOOL p12, BOOL p13);

	// TODO try to set vehicle controls (e.g. rotors half speed at start

	//SIMULATE_PLAYER_INPUT_GAIT(Player player, float amount, int gaitType, float speed, BOOL p4, BOOL p5) { invoke<Void>(0x477D5D63E63ECA5D, player, amount, gaitType, speed, p4, p5); } // 0x477D5D63E63ECA5D 0x0D77CC34
	//static void RESET_PLAYER_INPUT_GAIT(Player player)

}

void Scenario::setCameraPositionAndRotation(float x, float y, float z, float rot_x, float rot_y, float rot_z) {
	exporter.setCameraPositionAndRotation(x, y, z, rot_x, rot_y, rot_z);
}


void Scenario::createVehicle(const char* model, float relativeForward, float relativeRight, float heading, int color, int color2, bool placeOnGround, bool withLifeJacketPed,
                             float speed, int drivingMode, int actorId) {
	log("Scenario::CreateVehicle");

	Vector3 currentForwardVector, currentRightVector, currentUpVector, currentPos;
	ENTITY::GET_ENTITY_MATRIX(m_ownVehicle, &currentForwardVector, &currentRightVector, &currentUpVector, &currentPos);

	Hash vehicleHash = GAMEPLAY::GET_HASH_KEY(const_cast<char*>(model));

	Vector3 pos;
	pos.x = currentPos.x + currentForwardVector.x * relativeForward + currentRightVector.x * relativeRight;
	pos.y = currentPos.y + currentForwardVector.y * relativeForward + currentRightVector.y * relativeRight;
	pos.z = currentPos.z + currentForwardVector.z * relativeForward + currentRightVector.z * relativeRight;

	if (placeOnGround) {
		float groundZ;
		GAMEPLAY::GET_GROUND_Z_FOR_3D_COORD(pos.x, pos.y, pos.z, &groundZ, false);
		float waterZ;
		WATER::GET_WATER_HEIGHT(pos.x, pos.y, pos.z, &waterZ);
		float heightZ = std::max(groundZ, waterZ);
		pos.z = heightZ;
	}


	STREAMING::REQUEST_MODEL(vehicleHash);
	LT_WAIT_UNTIL(STREAMING::HAS_MODEL_LOADED(vehicleHash), 300, "spawned vehicle model");
	Vehicle tempV = VEHICLE::CREATE_VEHICLE(vehicleHash, pos.x, pos.y, pos.z, heading, FALSE, FALSE);
	if (color != -1) {
		VEHICLE::SET_VEHICLE_COLOURS(tempV, color, color2);
	}

	if (placeOnGround) {
		VEHICLE::SET_VEHICLE_ON_GROUND_PROPERLY(tempV);
	}

	Ped tempPed;
	if (withLifeJacketPed) {
		Hash modelHash = 0x0b4a6862;
		STREAMING::REQUEST_MODEL(modelHash);
		LT_WAIT_UNTIL(STREAMING::HAS_MODEL_LOADED(modelHash), 300, "ped model");
		tempPed = PED::CREATE_PED(4, modelHash, pos.x, pos.y, pos.z, heading, FALSE, FALSE);
		PED::SET_PED_COMPONENT_VARIATION(tempPed, 9, 1, 0, 2);
	}
	else {
		tempPed = PED::CREATE_RANDOM_PED(pos.x, pos.y, pos.z);
	}
	PED::SET_PED_INTO_VEHICLE(tempPed, tempV, -1);

	WAIT(0);
	// [longtail] speed and drivingMode were hard-coded to 2.0f / 16777216 upstream,
	// which makes every spawned vehicle crawl. They are now client-controlled, so a
	// spawned actor can be oncoming traffic, a cut-in, or a red-light runner.
	AI::TASK_VEHICLE_DRIVE_WANDER(tempPed, tempV, speed, drivingMode);

	SpawnedPed sp;
	sp.ped = tempPed;
	sp.spawntime = time(NULL);
	sp.actorId = actorId;
	spawnedPeds.push_back(sp);

	SpawnedVehicle sv;
	sv.vehicle = tempV;
	sv.spawntime = time(NULL);
	sv.actorId = actorId;
	spawnedVehicles.push_back(sv);


}

void Scenario::createPed(const char* model, float relativeForward, float relativeRight, float relativeUp, float heading, bool placeOnGround, const char* animDict, const char* animName, int task) {
	log("Scenario::createPed");
	std::string s = model;
	unsigned int modelInt;
	std::stringstream ss;
	ss << std::hex << model;
	ss >> modelInt;
	Hash modelHash = (Hash)modelInt;

	Vector3 currentForwardVector, currentRightVector, currentUpVector, currentPos;
	ENTITY::GET_ENTITY_MATRIX(m_ownVehicle, &currentForwardVector, &currentRightVector, &currentUpVector, &currentPos);

    Vector3 pos;
    pos.x = currentPos.x + currentForwardVector.x * relativeForward + currentRightVector.x * relativeRight + currentUpVector.x * relativeUp;
    pos.y = currentPos.y + currentForwardVector.y * relativeForward + currentRightVector.y * relativeRight + currentUpVector.y * relativeUp;
    pos.z = currentPos.z + currentForwardVector.z * relativeForward + currentRightVector.z * relativeRight + currentUpVector.z * relativeUp;

	if (placeOnGround) {
		float groundZ;
		GAMEPLAY::GET_GROUND_Z_FOR_3D_COORD(pos.x, pos.y, pos.z, &groundZ, false);
		float waterZ;
		WATER::GET_WATER_HEIGHT(pos.x, pos.y, pos.z, &waterZ);
		float heightZ = std::max(groundZ, waterZ);

		pos.z = heightZ-0.5;
	}

	STREAMING::REQUEST_MODEL(modelHash);
	while (!STREAMING::HAS_MODEL_LOADED(modelHash)) WAIT(0);
	Ped tempPed = PED::CREATE_PED(4, modelHash, pos.x, pos.y, pos.z, heading, FALSE, FALSE);
	if (placeOnGround) {
		OBJECT::PLACE_OBJECT_ON_GROUND_PROPERLY(tempPed);
	}
	
	// This is a dirty fix, to only spawn lifeguards with LifeJackets
	if (modelHash == 0x0b4a6862) {
		PED::SET_PED_COMPONENT_VARIATION(tempPed, 9, 1, 0, 2);
	}

	WAIT(0); 
	
	if (strlen(animDict) != 0 && strlen(animName) != 0) {
		AI::CLEAR_PED_TASKS(tempPed);
		PCHAR animDict1 = const_cast<PCHAR>(animDict);
		PCHAR animName1 = const_cast<PCHAR>(animName);

		STREAMING::REQUEST_ANIM_DICT(animDict1);
		while (!STREAMING::HAS_ANIM_DICT_LOADED(animDict1)) {
			WAIT(0);
		}

		AI::TASK_PLAY_ANIM(tempPed, animDict1, animName1, 4.0f, -4.0f, -1, 1, 0, false, false, false);
	}
	else {
		AI::TASK_WANDER_STANDARD(tempPed, 10.0f, 10);
	}
	
	SpawnedPed sp;
	sp.ped = tempPed;
	sp.spawntime = time(NULL);
	spawnedPeds.push_back(sp);

	// [longtail] task != 0 makes the ped actually DO something. 1 = walk straight
	// across the road in front of the ego, which is the dart-out PedestrianHazard
	// was always meant to produce.
	if (task != 0 && ENTITY::DOES_ENTITY_EXIST(tempPed)) {
		Vector3 pp = ENTITY::GET_ENTITY_COORDS(tempPed, TRUE);
		Vector3 f, r, u, epos;
		ENTITY::GET_ENTITY_MATRIX(m_ownVehicle, &f, &r, &u, &epos);
		float span = 14.0f;
		Vector3 dst;
		dst.x = pp.x - r.x * span; dst.y = pp.y - r.y * span; dst.z = pp.z;
		AI::TASK_GO_STRAIGHT_TO_COORD(tempPed, dst.x, dst.y, dst.z, 2.0f, -1, 0.0f, 0.0f);
		PED::SET_PED_KEEP_TASK(tempPed, TRUE);
		PED::SET_PED_FLEE_ATTRIBUTES(tempPed, 0, FALSE);   // do not scatter on sight
	}
}

void Scenario::despawnSpawnedObjectsAfterTime() {
	time_t currentTime = time(NULL);
	std::vector<SpawnedPed> newSpawnedPeds;
	std::vector<SpawnedVehicle> newSpawnedVehicles;

	for (SpawnedPed sp : spawnedPeds) {
		if (difftime(currentTime, sp.spawntime) > spawnedEntitiesDespawnSeconds) {
			PED::DELETE_PED(&sp.ped);
		}
		else {
			newSpawnedPeds.push_back(sp);
		}
	}
	for (SpawnedVehicle sv : spawnedVehicles) {
		if (difftime(currentTime, sv.spawntime) > spawnedEntitiesDespawnSeconds) {
			VEHICLE::DELETE_VEHICLE(&sv.vehicle);
		}
		else {
			newSpawnedVehicles.push_back(sv);
		}
	}
	spawnedPeds = newSpawnedPeds;
	spawnedVehicles = newSpawnedVehicles;

}


//void Scenario::createVehicles() {
//    setPosition();
//    if ((stationaryScene || TRUPERCEPT_SCENARIO) && !vehicles_created) {
//        log("Creating peds");
//        for (int i = 0; i < pedsToCreate.size(); i++) {
//            PedToCreate p = pedsToCreate[i];
//            createPed(p.model, p.forward, p.right, p.heading, i);
//        }
//        log("Creating vehicles");
//        for (int i = 0; i < vehiclesToCreate.size(); i++) {
//            VehicleToCreate v = vehiclesToCreate[i];
//            createVehicle(v.model.c_str(), v.forward, v.right, v.heading, v.color, v.color2);
//        }
//        vehicles_created = true;
//    }
//}


void Scenario::setWeather(const char* weather) {
	GAMEPLAY::SET_WEATHER_TYPE_NOW_PERSIST((char*)weather);
}

void Scenario::setClockTime(int hour, int minute, int second) {
	TIME::SET_CLOCK_TIME(hour, minute, second);
}

////Saves the position and vectors of the capture vehicle
//void Scenario::setPosition() {
//    //NOTE: The forward and right vectors are swapped (compared to native function labels) to keep consistency with coordinate system
//    ENTITY::GET_ENTITY_MATRIX(m_ownVehicle, &currentForwardVector, &currentRightVector, &currentUpVector, &currentPos); //Blue or red pill
//}


//void Scenario::drawBoxes(Vector3 BLL, Vector3 FUR, Vector3 dim, Vector3 upVector, Vector3 rightVector, Vector3 forwardVector, Vector3 position, int colour) {
//    //log("Inside draw boxes");
//    if (showBoxes) {
//        log("Inside show boxes");
//        Vector3 edge1 = BLL;
//        Vector3 edge2;
//        Vector3 edge3;
//        Vector3 edge4;
//        Vector3 edge5 = FUR;
//        Vector3 edge6;
//        Vector3 edge7;
//        Vector3 edge8;
//
//        int green = colour * 255;
//        int blue = abs(colour - 1) * 255;
//
//        edge2.x = edge1.x + 2 * dim.y*rightVector.x;
//        edge2.y = edge1.y + 2 * dim.y*rightVector.y;
//        edge2.z = edge1.z + 2 * dim.y*rightVector.z;
//
//        edge3.x = edge2.x + 2 * dim.z*upVector.x;
//        edge3.y = edge2.y + 2 * dim.z*upVector.y;
//        edge3.z = edge2.z + 2 * dim.z*upVector.z;
//
//        edge4.x = edge1.x + 2 * dim.z*upVector.x;
//        edge4.y = edge1.y + 2 * dim.z*upVector.y;
//        edge4.z = edge1.z + 2 * dim.z*upVector.z;
//
//        edge6.x = edge5.x - 2 * dim.y*rightVector.x;
//        edge6.y = edge5.y - 2 * dim.y*rightVector.y;
//        edge6.z = edge5.z - 2 * dim.y*rightVector.z;
//
//        edge7.x = edge6.x - 2 * dim.z*upVector.x;
//        edge7.y = edge6.y - 2 * dim.z*upVector.y;
//        edge7.z = edge6.z - 2 * dim.z*upVector.z;
//
//        edge8.x = edge5.x - 2 * dim.z*upVector.x;
//        edge8.y = edge5.y - 2 * dim.z*upVector.y;
//        edge8.z = edge5.z - 2 * dim.z*upVector.z;
//
//        GRAPHICS::DRAW_LINE(edge1.x, edge1.y, edge1.z, edge2.x, edge2.y, edge2.z, 0, green, blue, 200);
//        GRAPHICS::DRAW_LINE(edge1.x, edge1.y, edge1.z, edge4.x, edge4.y, edge4.z, 0, green, blue, 200);
//        GRAPHICS::DRAW_LINE(edge2.x, edge2.y, edge2.z, edge3.x, edge3.y, edge3.z, 0, green, blue, 200);
//        GRAPHICS::DRAW_LINE(edge3.x, edge3.y, edge3.z, edge4.x, edge4.y, edge4.z, 0, green, blue, 200);
//
//        GRAPHICS::DRAW_LINE(edge5.x, edge5.y, edge5.z, edge6.x, edge6.y, edge6.z, 0, green, blue, 200);
//        GRAPHICS::DRAW_LINE(edge5.x, edge5.y, edge5.z, edge8.x, edge8.y, edge8.z, 0, green, blue, 200);
//        GRAPHICS::DRAW_LINE(edge6.x, edge6.y, edge6.z, edge7.x, edge7.y, edge7.z, 0, green, blue, 200);
//        GRAPHICS::DRAW_LINE(edge7.x, edge7.y, edge7.z, edge8.x, edge8.y, edge8.z, 0, green, blue, 200);
//
//        GRAPHICS::DRAW_LINE(edge1.x, edge1.y, edge1.z, edge7.x, edge7.y, edge7.z, 0, green, blue, 200);
//        GRAPHICS::DRAW_LINE(edge2.x, edge2.y, edge2.z, edge8.x, edge8.y, edge8.z, 0, green, blue, 200);
//        GRAPHICS::DRAW_LINE(edge3.x, edge3.y, edge3.z, edge5.x, edge5.y, edge5.z, 0, green, blue, 200);
//        GRAPHICS::DRAW_LINE(edge4.x, edge4.y, edge4.z, edge6.x, edge6.y, edge6.z, 0, green, blue, 200);
//        WAIT(0);
//    }
//}




// ============================================================================
// [longtail] Long-tail event orchestration
// ============================================================================

// Apply only the guards the client asked for. Upstream applied all of them.
// [longtail] Upstream only ever configured the EGO's driver. The surrounding
// traffic keeps stock GTA behaviour -- competent, cautious drivers -- which is why
// collisions look tame: the ego misbehaves and everyone else politely avoids it.
// Push the whole local population toward reckless so the scene itself is hostile.
// [longtail] GTA keeps pedestrians on pavements, so raising ped density alone
// just crowds the sidewalk -- they never enter the road and never interact with
// the ego. Send a fraction of those ahead of the ego across its path, and stop
// them fleeing at the sight of a car so they are actually in the way.
void Scenario::makePedsInteractive() {
	if (!m_pedInteraction || !ENTITY::DOES_ENTITY_EXIST(m_ownVehicle)) return;
	if ((m_aggroTick % 30) != 0) return;        // shares the traffic tick counter

	Vector3 f, r, u, epos;
	ENTITY::GET_ENTITY_MATRIX(m_ownVehicle, &f, &r, &u, &epos);

	const int MAXP = 128;
	int peds[MAXP];
	int n = worldGetAllPeds(peds, MAXP);
	for (int i = 0; i < n; ++i) {
		Ped p = peds[i];
		if (!ENTITY::DOES_ENTITY_EXIST(p) || PED::IS_PED_A_PLAYER(p)) continue;
		if (PED::IS_PED_IN_ANY_VEHICLE(p, FALSE)) continue;

		Vector3 pp = ENTITY::GET_ENTITY_COORDS(p, TRUE);
		float dx = pp.x - epos.x, dy = pp.y - epos.y;
		float ahead = dx * f.x + dy * f.y;       // metres in front of the ego
		float dist2 = dx * dx + dy * dy;
		if (ahead < 8.0f || dist2 > 3600.0f) continue;   // 8..60 m ahead only

		// Don't scatter the moment a car appears -- that is what makes GTA crowds
		// evaporate exactly when the interesting thing is about to happen.
		PED::SET_PED_FLEE_ATTRIBUTES(p, 0, FALSE);
		PED::SET_PED_CONFIG_FLAG(p, 32, FALSE);

		if (((rand() % 100) / 100.0f) < m_pedCrossChance) {
			float span = 14.0f;
			AI::TASK_GO_STRAIGHT_TO_COORD(p, pp.x - r.x * span, pp.y - r.y * span, pp.z,
			                              2.0f, -1, 0.0f, 0.0f);
			PED::SET_PED_KEEP_TASK(p, TRUE);
		}
	}
}

// [longtail] ------------------------------------------------------------------
// Road leash: keep the ego on the vehicle node network.
//
// Two independent things put the ego into a field:
//   1. a driving style with IGNORE_ROADS (4194304) or IGNORE_ALL_PATHING
//      (16777216) set -- the wander task then has no obligation to the network
//      at all, and 15 s of dirt is a perfectly valid execution of it;
//   2. a collision that knocks the car off the carriageway, after which the
//      wander task happily resumes from wherever it landed.
//
// (1) is fixed by masking the bits out of whatever the client asks for; (2)
// needs an active recovery, because no style flag will bring the car back.
//
// ⚠ Do NOT reach for a "is this point on a road" native here. The one that is
// actually present in 1.0.3889.0 and that we already exercise elsewhere is
// GET_CLOSEST_VEHICLE_NODE; distance to the nearest node is a continuous
// measure, which is what a leash with hysteresis needs anyway. Node positions
// sit on the carriageway centreline, so the threshold has to clear half the
// width of a wide road (~10 m) plus slack -- 18 m, not 5 m.

// Bits that release the driver from the road network. Masked out of the ego's
// style unconditionally when the leash is on: no long-tail scenario we generate
// needs them, and they are the single largest source of off-road footage.
static const int LT_OFFROAD_BITS = 4194304 /*IGNORE_ROADS*/ | 16777216 /*IGNORE_ALL_PATHING*/;

int Scenario::egoDrivingModeOnRoad() const {
	if (_drivingMode < 0) return _drivingMode;          // manual control
	if (!m_roadLeash) return _drivingMode;
	return _drivingMode & ~LT_OFFROAD_BITS;
}

void Scenario::setRoadLeash(bool enabled, float dist, float seconds) {
	m_roadLeash = enabled;
	if (dist > 0.0f) m_roadLeashDist = dist;
	if (seconds > 0.0f) m_roadLeashSeconds = seconds;
	m_leashStrikes = 0;
	m_leashActive = false;
	log("[leash] enabled=" + std::to_string((int)m_roadLeash) +
	    " dist=" + std::to_string(m_roadLeashDist) +
	    " seconds=" + std::to_string(m_roadLeashSeconds));
}

void Scenario::enforceRoadLeash() {
	if (!m_roadLeash) return;
	if (_drivingMode < 0) return;                       // client is driving
	if (m_ownVehicle == NULL || !ENTITY::DOES_ENTITY_EXIST(m_ownVehicle)) return;

	// Check a few times a second, not every frame: GET_CLOSEST_VEHICLE_NODE is a
	// pathfind query and we already run three other per-frame sweeps.
	const int CHECK_EVERY = 8;
	if ((++m_leashTick % CHECK_EVERY) != 0) return;

	Vector3 pos = ENTITY::GET_ENTITY_COORDS(m_ownVehicle, false);
	Vector3 node; node.x = node.y = node.z = 0.0f;
	BOOL got = PATHFIND::GET_CLOSEST_VEHICLE_NODE(pos.x, pos.y, pos.z, &node, 1, 3.0f, 0.0f);
	if (!got) {
		// No node reachable at all -- deep off-network. Treat as a strike but
		// we have nowhere to steer to, so only count it.
		m_lastRoadDist = 9999.0f;
		++m_leashStrikes;
		return;
	}

	const float dx = node.x - pos.x, dy = node.y - pos.y;
	const float dist = sqrtf(dx * dx + dy * dy);
	m_lastRoadDist = dist;

	// Sustained, not instantaneous: crossing a wide junction or clipping a kerb
	// briefly reads as "off network" and is exactly the driving we want to keep.
	// ~30 fps game tick / CHECK_EVERY gives the checks-per-second.
	int strikesNeeded = (int)(m_roadLeashSeconds * 30.0f / (float)CHECK_EVERY);
	if (strikesNeeded < 2) strikesNeeded = 2;

	Ped driver = PLAYER::PLAYER_PED_ID();

	if (dist > m_roadLeashDist) {
		if (++m_leashStrikes < strikesNeeded) return;
		if (m_leashActive) return;                      // already recovering

		// Steer back onto the network. TASK_VEHICLE_DRIVE_TO_COORD with a
		// road-respecting style, not the clip's style -- the clip's style is what
		// put us here.
		Hash model = ENTITY::GET_ENTITY_MODEL(m_ownVehicle);
		const int RECOVER_STYLE = 786603;               // NORMAL: obeys roads
		AI::CLEAR_PED_TASKS(driver);
		AI::TASK_VEHICLE_DRIVE_TO_COORD(driver, m_ownVehicle, node.x, node.y, node.z,
		                                _setSpeed > 5.0f ? _setSpeed : 15.0f, Any(1.f),
		                                model, RECOVER_STYLE, 4.0f, true);
		m_leashActive = true;
		++m_leashRecoveries;
		log("[leash] off-network " + std::to_string(dist) + " m -> recovering to node");
		return;
	}

	m_leashStrikes = 0;
	if (m_leashActive) {
		// Hysteresis: only hand control back once we are comfortably on, so the
		// car does not ping-pong between the two tasks at the threshold.
		//
		// ⚠ DEAD BAND. Requiring dist <= half the leash distance leaves 9-18 m
		// where neither branch runs: the drive-to-coord has already COMPLETED, so
		// the ped has no task at all, and wander is never restored. The car simply
		// parks. Observed recoveries all landed under 9 m so it has not bitten yet,
		// but a car that stops moving is also a car whose distance stops changing,
		// which is precisely the state that would never leave the band.
		// Hand control back on being stopped, too.
		if (dist > m_roadLeashDist * 0.5f &&
		    ENTITY::GET_ENTITY_SPEED(m_ownVehicle) > 1.0f) return;
		AI::CLEAR_PED_TASKS(driver);
		AI::TASK_VEHICLE_DRIVE_WANDER(driver, m_ownVehicle, _setSpeed, egoDrivingModeOnRoad());
		m_leashActive = false;
		log("[leash] back on network at " + std::to_string(dist) + " m; wander resumed");
	}
}

// [longtail] ------------------------------------------------------------------
// Scene seeding: place traffic and VRUs rather than hoping the game spawns them.
//
// ★ Measured over 13 clips: damage to other vehicles scales ~4.7x with how many
// are in scene -- mean 0.7 damaged when <=4 were nearby, 3.3 when >=8 -- and the
// MEDIAN clip had only 4 vehicles within 60 m. Density was the binding constraint
// on chaos, not driver aggression.
//
// ⚠ SET_VEHICLE_DENSITY_MULTIPLIER_THIS_FRAME cannot fix that. It SCALES GTA's
// population model rather than inventing traffic, so 3x of a near-zero base is
// still near zero -- which is why a canyon road at 2.73x yielded one vehicle.

// A spread of body types so a seeded scene does not look like a fleet. Kept to
// common models that are cheap to stream; an unavailable model just fails the
// REQUEST and that slot is skipped.
static const char *LT_SEED_VEHICLES[] = {
	"blista", "futo", "premier", "primo", "stanier", "washington", "tailgater",
	"sultan", "buffalo", "asea", "baller", "granger", "patriot", "seminole",
	"rumpo", "burrito", "minivan", "bison", "pounder", "mule", "taxi", "bus",
};
static const int LT_SEED_VEHICLE_N = sizeof(LT_SEED_VEHICLES) / sizeof(LT_SEED_VEHICLES[0]);

// Ambient pedestrian models that exist in every 1.0.3889.0 install. The point is
// not variety, it is REPRODUCIBILITY: CREATE_RANDOM_PED asks the game, and the
// game's choice cannot be seeded. Picking from a fixed list by (seed, index) means
// the same scene gets the same people in every variation and on every rerun.
static const char *LT_SEED_PEDS[] = {
	"a_m_y_business_01", "a_f_y_business_01", "a_m_m_business_01", "a_f_m_business_02",
	"a_m_y_hipster_01",  "a_f_y_hipster_01",  "a_m_y_skater_01",   "a_f_y_tourist_01",
	"a_m_m_tourist_01",  "a_m_y_runner_01",   "a_f_y_runner_01",   "a_m_m_eastsa_01",
	"a_f_m_eastsa_01",   "a_m_y_downtown_01", "a_f_y_soucent_01",  "a_m_m_soucent_01",
	"a_m_y_genstreet_01","a_f_y_genhot_01",   "a_m_m_afriamer_01", "a_m_y_beach_01",
};
static const int LT_SEED_PED_N = sizeof(LT_SEED_PEDS) / sizeof(LT_SEED_PEDS[0]);

Ped Scenario::createSeededPed(float x, float y, float z, float heading, unsigned idx) {
	// A cheap integer hash so consecutive indices do not walk the list in order
	// (which would make every scene's first three peds the same three models).
	unsigned h = (m_seedValue * 2654435761u) ^ (idx * 40503u);
	const char *name = LT_SEED_PEDS[h % LT_SEED_PED_N];
	Hash model = GAMEPLAY::GET_HASH_KEY(const_cast<char*>(name));
	STREAMING::REQUEST_MODEL(model);
	LT_WAIT_UNTIL(STREAMING::HAS_MODEL_LOADED(model), 200, "seeded ped model");
	if (!STREAMING::HAS_MODEL_LOADED(model)) {
		// Degrade rather than lose the ped, and say so: a name missing from this
		// build silently reintroduces the game's own randomness.
		log(std::string("[seed] ped model '") + name + "' did not load; falling back to random");
		return PED::CREATE_RANDOM_PED(x, y, z);
	}
	Ped p = PED::CREATE_PED(4, model, x, y, z, heading, FALSE, FALSE);
	STREAMING::SET_MODEL_AS_NO_LONGER_NEEDED(model);
	return p;
}

static const char *LT_SEED_BIKES[] = { "bmx", "scorcher", "cruiser", "fixter", "tribike" };
static const int LT_SEED_BIKE_N = sizeof(LT_SEED_BIKES) / sizeof(LT_SEED_BIKES[0]);

void Scenario::seedScene(int vehicles, int peds, int cyclists, float radius,
                         unsigned seed, bool clearAmbient) {
	if (m_ownVehicle == NULL || !ENTITY::DOES_ENTITY_EXIST(m_ownVehicle)) return;
	Vector3 ep = ENTITY::GET_ENTITY_COORDS(m_ownVehicle, false);
	m_seedValue = seed;
	m_suppressAmbient = clearAmbient;

	if (clearAmbient) {
		// ★ Make the seeded population THE population. GTA exposes no way to seed
		// its ambient spawner, so the only route to a reproducible scene is to
		// remove what it already placed and hold its density at zero for the clip
		// (applyDensityThisFrame honours m_suppressAmbient). The ego's own vehicle
		// is a script entity and survives CLEAR_AREA.
		const float R = radius + 60.0f;
		GAMEPLAY::CLEAR_AREA_OF_VEHICLES(ep.x, ep.y, ep.z, R, FALSE, FALSE, FALSE, FALSE, FALSE);
		GAMEPLAY::CLEAR_AREA_OF_PEDS(ep.x, ep.y, ep.z, R, 0);
	}

	int placedV = 0, placedP = 0, placedC = 0;

	// --- vehicles + cyclists, both on the node network -----------------------
	// ⚠ Place on vehicle NODES, not on random offsets from the ego: a car dropped
	// at an arbitrary point lands in a wall, on a roof, or in the sea, and the
	// heading is meaningless. Walking outward through the nth-closest nodes gives
	// legal road positions with a legal heading, which is the same reasoning the
	// ego's own spawn uses.
	const int wantWheeled = vehicles + cyclists;
	for (int n = 2; n <= 60 && (placedV + placedC) < wantWheeled; ++n) {
		Vector3 np; float nh = 0.0f;
		Any unused = 0;
		if (!PATHFIND::GET_NTH_CLOSEST_VEHICLE_NODE_WITH_HEADING(
				ep.x, ep.y, ep.z, n, &np, &nh, &unused, 1, 3.0f, 0.0f)) continue;

		const float dx = np.x - ep.x, dy = np.y - ep.y;
		const float d = sqrtf(dx * dx + dy * dy);
		// Not on top of the ego (it would spawn inside us and explode) and not so
		// far that it is outside the clip entirely.
		if (d < 22.0f || d > radius) continue;

		const bool asBike = (placedC < cyclists) && ((n % 3) == 0);
		const char *model = asBike
			? LT_SEED_BIKES[(n * 7) % LT_SEED_BIKE_N]
			: LT_SEED_VEHICLES[(n * 5) % LT_SEED_VEHICLE_N];
		Hash h = GAMEPLAY::GET_HASH_KEY(const_cast<char*>(model));
		STREAMING::REQUEST_MODEL(h);
		LT_WAIT_UNTIL(STREAMING::HAS_MODEL_LOADED(h), 200, "seed model");
		if (!STREAMING::HAS_MODEL_LOADED(h)) continue;

		Vehicle v = VEHICLE::CREATE_VEHICLE(h, np.x, np.y, np.z, nh, FALSE, FALSE);
		if (v == 0 || !ENTITY::DOES_ENTITY_EXIST(v)) continue;
		VEHICLE::SET_VEHICLE_ON_GROUND_PROPERLY(v);

		Ped drv = createSeededPed(np.x, np.y, np.z, nh, (unsigned)n);
		if (drv != 0 && ENTITY::DOES_ENTITY_EXIST(drv)) {
			PED::SET_PED_INTO_VEHICLE(drv, v, -1);
			AI::TASK_VEHICLE_DRIVE_WANDER(drv, v, m_npcCruiseSpeed, m_npcDrivingStyle);
			PED::SET_PED_KEEP_TASK(drv, TRUE);
			PED::SET_DRIVER_AGGRESSIVENESS(drv, m_npcAggressiveness);
			PED::SET_DRIVER_ABILITY(drv, m_npcAbility);
			PED::SET_PED_STEERS_AROUND_VEHICLES(drv, m_npcSteersAround ? TRUE : FALSE);
			PED::SET_PED_STEERS_AROUND_PEDS(drv, m_npcSteersAround ? TRUE : FALSE);
			PED::SET_PED_STEERS_AROUND_OBJECTS(drv, m_npcSteersAround ? TRUE : FALSE);
			SpawnedPed sp; sp.ped = drv; sp.spawntime = time(NULL); sp.actorId = -1;
			spawnedPeds.push_back(sp);
		}
		SpawnedVehicle sv; sv.vehicle = v; sv.spawntime = time(NULL); sv.actorId = -1;
		spawnedVehicles.push_back(sv);
		STREAMING::SET_MODEL_AS_NO_LONGER_NEEDED(h);
		if (asBike) ++placedC; else ++placedV;
	}

	// --- VRUs: pedestrians -------------------------------------------------
	for (int i = 0; i < peds * 4 && placedP < peds; ++i) {
		if (placeVRU(i, (i % 3) == 0) != 0) ++placedP;
	}

	// The requested ped count becomes the SUSTAINED target for the clip, not a
	// one-off: topUpVRUs() keeps roughly this many in the forward cone as the ego
	// drives through and past them.
	m_vruTarget = peds > 0 ? (peds < 3 ? peds : peds / 2) : 0;
	m_vruSpawnCursor = placedP;

	log("[seed] placed " + std::to_string(placedV) + " vehicles, " +
	    std::to_string(placedC) + " cyclists, " + std::to_string(placedP) +
	    " peds; sustaining " + std::to_string(m_vruTarget) + " in the forward cone");
}

// [longtail] Place one VRU in the ego's forward cone.
//
// ★ A forward cone, not a scatter around the ego: scattered peds land behind and
// beside the car where they never enter frame and never interact, and the point
// of a VRU in this dataset is that the ego has to react to it. The cone is also
// deliberately TIGHT -- 12-45 m ahead, +-14 m lateral. A ped at 70 m is a speck,
// and at +-22 m it is outside the camera's useful field.
//
// ⚠ GET_SAFE_COORD_FOR_PED is tried first so they land on pavements, but it
// returns nothing in plenty of places (an elevated highway, a port road). Falling
// back to ground height at the requested point beats shipping a clip with no VRUs.
Ped Scenario::placeVRU(int idx, bool crossing) {
	if (m_ownVehicle == NULL || !ENTITY::DOES_ENTITY_EXIST(m_ownVehicle)) return 0;
	Vector3 fwd, rgt, up, epos;
	ENTITY::GET_ENTITY_MATRIX(m_ownVehicle, &fwd, &rgt, &up, &epos);

	const float span = m_vruFar - m_vruNear;
	const float ahead = m_vruNear + (float)((idx * 17) % (int)(span < 1.0f ? 1.0f : span));
	const float side  = (float)(((idx * 23) % (int)(m_vruHalfWidth * 2.0f + 1.0f))
	                            - m_vruHalfWidth);
	Vector3 want;
	want.x = epos.x + fwd.x * ahead + rgt.x * side;
	want.y = epos.y + fwd.y * ahead + rgt.y * side;
	want.z = epos.z;

	Vector3 at = want;
	if (!PATHFIND::GET_SAFE_COORD_FOR_PED(want.x, want.y, want.z, TRUE, &at, 16)) {
		float gz = want.z;
		if (!GAMEPLAY::GET_GROUND_Z_FOR_3D_COORD(want.x, want.y, want.z + 10.0f, &gz, false))
			return 0;
		at = want; at.z = gz;
	}
	// A safe-coord snap can land somewhere irrelevant; a ped outside the cone is
	// a wasted slot, not a VRU.
	const float dxp = at.x - epos.x, dyp = at.y - epos.y;
	if (sqrtf(dxp * dxp + dyp * dyp) > m_vruFar * 1.6f) return 0;

	Ped p = createSeededPed(at.x, at.y, at.z, 0.0f, 1000u + (unsigned)idx);
	if (p == 0 || !ENTITY::DOES_ENTITY_EXIST(p)) return 0;
	PED::SET_PED_RANDOM_COMPONENT_VARIATION(p, FALSE);
	// Exposed and staying that way.
	PED::SET_PED_FLEE_ATTRIBUTES(p, 0, FALSE);
	PED::SET_PED_CONFIG_FLAG(p, 17, TRUE);     // can be knocked down by a vehicle
	PED::SET_BLOCKING_OF_NON_TEMPORARY_EVENTS(p, TRUE);
	ENTITY::SET_ENTITY_INVINCIBLE(p, FALSE);
	PED::SET_PED_CAN_RAGDOLL(p, TRUE);

	if (crossing) {
		Vector3 across;
		across.x = at.x - rgt.x * (side > 0 ? 28.0f : -28.0f);
		across.y = at.y - rgt.y * (side > 0 ? 28.0f : -28.0f);
		across.z = at.z;
		AI::TASK_GO_STRAIGHT_TO_COORD(p, across.x, across.y, across.z, 2.2f, -1, 0.0f, 0.0f);
	} else {
		AI::TASK_WANDER_STANDARD(p, 10.0f, 10);
	}
	PED::SET_PED_KEEP_TASK(p, TRUE);
	SpawnedPed sp; sp.ped = p; sp.spawntime = time(NULL); sp.actorId = -1;
	spawnedPeds.push_back(sp);
	return p;
}

// [longtail] ★ Keep the forward cone populated for the WHOLE clip.
//
// ⚠ At 25 m/s the ego crosses a 45 m cone in under two seconds, so a one-shot
// placement leaves the rest of the clip empty. Observed directly: a clip seeded
// with 18 pedestrians showed none in any sampled frame, because the car had
// already driven past all of them before recording got going.
void Scenario::topUpVRUs() {
	if (m_vruTarget <= 0) return;
	if (m_ownVehicle == NULL || !ENTITY::DOES_ENTITY_EXIST(m_ownVehicle)) return;

	// A few times a second is plenty and keeps the ped sweep off the hot path.
	if ((++m_vruTick % 20) != 0) return;

	Vector3 fwd, rgt, up, epos;
	ENTITY::GET_ENTITY_MATRIX(m_ownVehicle, &fwd, &rgt, &up, &epos);

	const int MAXP = 128;
	int peds[MAXP];
	int n = worldGetAllPeds(peds, MAXP);
	int inCone = 0;
	for (int i = 0; i < n; ++i) {
		Ped p = peds[i];
		if (!ENTITY::DOES_ENTITY_EXIST(p) || PED::IS_PED_A_PLAYER(p)) continue;
		if (PED::IS_PED_IN_ANY_VEHICLE(p, FALSE)) continue;
		Vector3 pp = ENTITY::GET_ENTITY_COORDS(p, false);
		const float dx = pp.x - epos.x, dy = pp.y - epos.y;
		const float along = dx * fwd.x + dy * fwd.y;
		const float lat   = dx * rgt.x + dy * rgt.y;
		if (along < m_vruNear * 0.5f || along > m_vruFar) continue;
		if (lat < -m_vruHalfWidth || lat > m_vruHalfWidth) continue;
		++inCone;
	}
	if (inCone >= m_vruTarget) return;

	// Place a couple per pass rather than the whole deficit at once: spawning is
	// not free, and a burst of peds appearing together reads as a crowd teleport.
	const int want = m_vruTarget - inCone;
	const int batch = want > 2 ? 2 : want;
	for (int k = 0; k < batch; ++k) {
		placeVRU(++m_vruSpawnCursor, (m_vruSpawnCursor % 3) == 0);
	}
}

// [longtail] ⚠ Measured tank_health_min across a whole set of collisions: 984.9
// of 1000, and on_fire false on every clip. GTA only ignites a vehicle when its
// engine health goes NEGATIVE, which a single road collision does not do -- so
// "collisions should set things on fire" never happened no matter how the tank
// was weakened. Ignite explicitly once a vehicle is wrecked instead.
void Scenario::makeTrafficAggressive() {
	if (!m_trafficAggression) return;

	// ⚠ This used to run only on the 10-second safety tick. GTA streams traffic in
	// continuously, so most vehicles around the ego during a 15s clip were never
	// touched at all -- which is why the traffic still behaved politely. Run it
	// often, but not every single frame (worldGetAllVehicles + per-ped natives is
	// not free).
	if ((++m_aggroTick % 10) != 0) return;

	const int MAXV = 128;
	int vehs[MAXV];
	int n = worldGetAllVehicles(vehs, MAXV);
	for (int i = 0; i < n; ++i) {
		Vehicle v = vehs[i];
		if (v == m_ownVehicle || !ENTITY::DOES_ENTITY_EXIST(v)) continue;
		Ped d = VEHICLE::GET_PED_IN_VEHICLE_SEAT(v, -1);
		if (!ENTITY::DOES_ENTITY_EXIST(d) || PED::IS_PED_A_PLAYER(d)) continue;

		PED::SET_DRIVER_AGGRESSIVENESS(d, m_npcAggressiveness);
		PED::SET_DRIVER_ABILITY(d, m_npcAbility);

		// ★ The direct levers. Style bits bias a drive task; these three switch off
		// the avoidance behaviour itself, and they hold regardless of what task the
		// ambient population system has the driver on. Without them a driver with a
		// no-avoidance style still steers around obstacles.
		PED::SET_PED_STEERS_AROUND_VEHICLES(d, m_npcSteersAround ? TRUE : FALSE);
		PED::SET_PED_STEERS_AROUND_PEDS(d, m_npcSteersAround ? TRUE : FALSE);
		PED::SET_PED_STEERS_AROUND_OBJECTS(d, m_npcSteersAround ? TRUE : FALSE);

		AI::SET_DRIVE_TASK_DRIVING_STYLE(d, m_npcDrivingStyle);
		AI::SET_DRIVE_TASK_CRUISE_SPEED(d, m_npcCruiseSpeed);
		AI::SET_DRIVE_TASK_MAX_CRUISE_SPEED(d, m_npcCruiseSpeed);

		// ⚠ This used to re-task ONLY vehicles below 2 m/s, i.e. the ones already
		// stopped. Every moving vehicle -- which is all of the traffic that matters
		// -- kept whatever task the ambient population gave it and only got the
		// SET_DRIVE_TASK_* flags applied on top. Re-task each vehicle once, tracked,
		// so it commits to the aggressive style without being reset every sweep.
		if (m_retasked.find((int)v) == m_retasked.end()) {
			AI::TASK_VEHICLE_DRIVE_WANDER(d, v, m_npcCruiseSpeed, m_npcDrivingStyle);
			PED::SET_PED_KEEP_TASK(d, TRUE);
			m_retasked.insert((int)v);
		}

		// And make them breakable, so contact actually does something.
		VEHICLE::SET_VEHICLE_CAN_BE_VISIBLY_DAMAGED(v, TRUE);
		ENTITY::SET_ENTITY_INVINCIBLE(v, FALSE);
		VEHICLE::SET_VEHICLE_TYRES_CAN_BURST(v, TRUE);
		VEHICLE::SET_VEHICLE_WHEELS_CAN_BREAK(v, TRUE);
		VEHICLE::SET_VEHICLE_HAS_STRONG_AXLES(v, FALSE);
		PED::SET_PED_CAN_BE_DRAGGED_OUT(d, TRUE);

		// ⚠ Measured: tank_health_min across a whole set of collisions was 984.9 of
		// 1000 -- nothing ever came close to igniting, so on_fire was false on every
		// clip. Allow tank fires and start the tank weakened so a hard impact can
		// actually produce one.
		VEHICLE::SET_DISABLE_VEHICLE_PETROL_TANK_FIRES(v, FALSE);
		VEHICLE::SET_DISABLE_VEHICLE_PETROL_TANK_DAMAGE(v, FALSE);
		if (VEHICLE::GET_VEHICLE_PETROL_TANK_HEALTH(v) > m_npcTankHealth) {
			VEHICLE::SET_VEHICLE_PETROL_TANK_HEALTH(v, m_npcTankHealth);
		}
	}
}

// [longtail] ★ Unstick the ego, whatever stopped it.
//
// Measured across four stalled clips: frames kept arriving at normal cadence
// (max dt ~2x median) while speed sat at zero. That rules out the plugin blocking
// the script thread -- a block stops FRAMES, not the car -- and leaves only one
// explanation: the ego has no drive task. GTA's wander task can simply end (no
// route, blocked by geometry, a scripted task that completed), and nothing was
// putting it back.
//
// Cause-agnostic on purpose. Chasing each individual way a task can end is a
// losing game; noticing that the car is stopped for no reason and re-tasking it
// covers all of them, including the ones not yet seen.
void Scenario::enforceEgoMobility() {
	if (!m_egoMobility) return;
	if (_drivingMode < 0) return;                    // client is driving
	if (m_ownVehicle == NULL || !ENTITY::DOES_ENTITY_EXIST(m_ownVehicle)) return;
	if (m_leashActive) return;                       // the leash owns the task

	const float speed = ENTITY::GET_ENTITY_SPEED(m_ownVehicle);
	std::clock_t now = std::clock();
	if (speed > 1.0f) { m_lastMovingClock = now; m_unstuckIssued = false; return; }
	if (m_lastMovingClock == 0) { m_lastMovingClock = now; return; }

	const float stoppedFor = (float)(now - m_lastMovingClock) / (float)CLOCKS_PER_SEC;
	if (stoppedFor < m_egoStuckSeconds) return;

	// ⚠ A wrecked ego coming to rest is the EXPECTED end of a collision clip, not a
	// fault -- the same distinction the stuck detector, the travel gate and the
	// rollover guard all had to learn. Do not drag a wreck back into traffic.
	if (VEHICLE::GET_VEHICLE_BODY_HEALTH(m_ownVehicle) < 900.0f) return;
	if (VEHICLE::IS_VEHICLE_ON_ALL_WHEELS(m_ownVehicle) == FALSE) return;

	if (m_unstuckIssued) return;                     // one attempt per stall
	AI::CLEAR_PED_TASKS(ped);
	AI::TASK_VEHICLE_DRIVE_WANDER(ped, m_ownVehicle, _setSpeed, egoDrivingModeOnRoad());
	m_unstuckIssued = true;
	++m_unstuckCount;
	m_lastMovingClock = now;                         // give it time to get going
	log("[unstick] ego stopped " + std::to_string(stoppedFor) +
	    "s with no damage -- re-issued wander (total " +
	    std::to_string(m_unstuckCount) + ")");
}

void Scenario::igniteWrecks() {
	if (!m_igniteWrecks) return;

	// ⚠ The ego was excluded here, which meant the `fire` OUTCOME label -- which
	// reads the EGO's on-fire state -- could never be produced. 745 ignitions were
	// logged across an overnight run and the label count was still zero. If burning
	// wrecks are wanted as a class, the ego has to be able to be one of them; the
	// player ped is invincible, so the car burns without a death cutscene.
	const int MAXV = 128;
	int vehs[MAXV];
	int n = worldGetAllVehicles(vehs, MAXV);
	for (int i = 0; i <= n; ++i) {
		Vehicle v = (i == n) ? (m_igniteEgo ? m_ownVehicle : 0) : vehs[i];
		if (v == 0 || !ENTITY::DOES_ENTITY_EXIST(v)) continue;
		if (m_ignited.find((int)v) != m_ignited.end()) continue;
		if (VEHICLE::GET_VEHICLE_BODY_HEALTH(v) > m_igniteBelowHealth) continue;
		if (FIRE::IS_ENTITY_ON_FIRE(v)) { m_ignited.insert((int)v); continue; }

		// ⚠ A vehicle only burns in GTA once its ENGINE health is negative. Setting
		// the body damage is not enough and neither is weakening the petrol tank --
		// measured tank_health_min 984.9/1000 across a whole set of collisions with
		// on_fire false everywhere. Push the engine under before asking for fire, or
		// START_ENTITY_FIRE lights a flame the engine immediately puts out.
		VEHICLE::SET_VEHICLE_PETROL_TANK_HEALTH(v, -1.0f);
		VEHICLE::SET_VEHICLE_ENGINE_HEALTH(v, -1.0f);
		Any fh = FIRE::START_ENTITY_FIRE(v);
		m_ignited.insert((int)v);

		// The return is a fire handle; 0 means the call did nothing. Log it with the
		// immediate readback, so "did ignition work" is answerable from the log
		// rather than inferred from an outcome count that has other reasons to be 0.
		const bool lit = (FIRE::IS_ENTITY_ON_FIRE(v) != 0);
		log("[fire] ignite " + std::string(v == m_ownVehicle ? "EGO" : "npc") +
		    " handle=" + std::to_string((int)fh) +
		    " onfire_now=" + std::to_string((int)lit) +
		    " body=" + std::to_string(VEHICLE::GET_VEHICLE_BODY_HEALTH(v)));
	}
}


void Scenario::applySurvivalSettings() {
	if (!ENTITY::DOES_ENTITY_EXIST(m_ownVehicle)) return;

	PLAYER::SET_EVERYONE_IGNORE_PLAYER(player, m_survival.everyoneIgnore ? TRUE : FALSE);
	PLAYER::SET_POLICE_IGNORE_PLAYER(player, m_survival.policeIgnore ? TRUE : FALSE);
	if (m_survival.policeIgnore) PLAYER::CLEAR_PLAYER_WANTED_LEVEL(player);

	// Seatbelt: FALSE on config flag 32 means "will NOT be ejected through the
	// windscreen". Keep it on -- an ejected ped detaches the ego loop from the car.
	PED::SET_PED_CONFIG_FLAG(ped, 32, m_survival.seatbelt ? FALSE : TRUE);

	const BOOL vInv = m_survival.vehicleInvincible ? TRUE : FALSE;
	VEHICLE::SET_VEHICLE_TYRES_CAN_BURST(m_ownVehicle, m_survival.vehicleInvincible ? FALSE : TRUE);
	VEHICLE::SET_VEHICLE_WHEELS_CAN_BREAK(m_ownVehicle, m_survival.vehicleInvincible ? FALSE : TRUE);
	VEHICLE::SET_VEHICLE_HAS_STRONG_AXLES(m_ownVehicle, vInv);
	VEHICLE::SET_VEHICLE_CAN_BE_VISIBLY_DAMAGED(m_ownVehicle, m_survival.vehicleInvincible ? FALSE : TRUE);
	ENTITY::SET_ENTITY_INVINCIBLE(m_ownVehicle, vInv);
	const int pf = m_survival.vehicleInvincible ? 1 : 0;
	ENTITY::SET_ENTITY_PROOFS(m_ownVehicle, pf, pf, pf, pf, pf, pf, pf, pf);

	// Player ped invincibility is kept independent of the vehicle: we want the car
	// to be destroyed without ever triggering the death/hospital cutscene, which
	// would take the camera away from us mid-clip.
	PLAYER::SET_PLAYER_INVINCIBLE(player, m_survival.playerInvincible ? TRUE : FALSE);

	PED::SET_DRIVER_AGGRESSIVENESS(ped, m_survival.aggressiveness);
	PED::SET_DRIVER_ABILITY(ped, m_survival.ability);
}

void Scenario::setSurvivalMode(bool policeIgnore, bool everyoneIgnore, bool playerInvincible,
                               bool vehicleInvincible, bool seatbelt, float aggressiveness, float ability) {
	log("Scenario::setSurvivalMode");
	m_survival.policeIgnore = policeIgnore;
	m_survival.everyoneIgnore = everyoneIgnore;
	m_survival.playerInvincible = playerInvincible;
	m_survival.vehicleInvincible = vehicleInvincible;
	m_survival.seatbelt = seatbelt;
	m_survival.aggressiveness = aggressiveness;
	m_survival.ability = ability;
	// Apply immediately rather than waiting for the next 10s tick, so a clip that
	// starts right after the call is not recorded under the previous settings.
	applySurvivalSettings();
}

// actorId -1 addresses the ego vehicle; >= 0 addresses a CreateVehicle actor.
bool Scenario::resolveActor(int actorId, Vehicle& vehOut, Ped& pedOut) {
	if (actorId < 0) {
		vehOut = m_ownVehicle;
		pedOut = ped;
		return ENTITY::DOES_ENTITY_EXIST(vehOut) != 0;
	}
	Vehicle v = NULL; Ped p = NULL;
	for (const SpawnedVehicle& sv : spawnedVehicles) if (sv.actorId == actorId) v = sv.vehicle;
	for (const SpawnedPed& sp : spawnedPeds)         if (sp.actorId == actorId) p = sp.ped;
	if (v == NULL || p == NULL) return false;
	if (!ENTITY::DOES_ENTITY_EXIST(v) || !ENTITY::DOES_ENTITY_EXIST(p)) return false;
	vehOut = v; pedOut = p;
	return true;
}

// TASK_VEHICLE_TEMP_ACTION: the cleanest primitive for a scripted swerve or
// brake. Action ids are the game's own (e.g. 1 = brake, 6/7 = hard turn L/R,
// 9/10 = swerve L/R); verify against NativeDB for your game build.
void Scenario::taskVehicleTempAction(int actorId, int action, int durationMs) {
	log("Scenario::taskVehicleTempAction");
	Vehicle v; Ped p;
	if (!resolveActor(actorId, v, p)) return;
	AI::TASK_VEHICLE_TEMP_ACTION(p, v, action, durationMs);
}

void Scenario::taskVehicleDriveToCoord(int actorId, float x, float y, float z, float speed, int drivingMode) {
	log("Scenario::taskVehicleDriveToCoord");
	Vehicle v; Ped p;
	if (!resolveActor(actorId, v, p)) return;
	Hash vehicleHash = ENTITY::GET_ENTITY_MODEL(v);
	AI::TASK_VEHICLE_DRIVE_TO_COORD(p, v, x, y, z, speed, Any(1.f), vehicleHash, drivingMode, 2.f, true);
}

// Re-task the ego with a new driving style mid-clip. This is how the ego itself
// runs a light or crosses into the oncoming lane: the style bitmask, not a
// separate scripted path.
void Scenario::setEgoDrivingMode(int drivingMode, float setSpeed) {
	log("Scenario::setEgoDrivingMode");
	_drivingMode = drivingMode;
	_setSpeed = setSpeed;
	if (!ENTITY::DOES_ENTITY_EXIST(m_ownVehicle)) return;
	AI::CLEAR_PED_TASKS(ped);
	m_leashStrikes = 0;
	m_leashActive = false;
	if (drivingMode >= 0) AI::TASK_VEHICLE_DRIVE_WANDER(ped, m_ownVehicle, setSpeed, egoDrivingModeOnRoad());
}


// [longtail] Rockstar Editor clip recording.
//
// A .clip is the game's own recording of the scene -- entity states, not
// pixels -- which the Rockstar Editor can replay in-engine later from any
// camera. Recording one alongside every captured clip makes the mp4 optional:
// the scene can be re-rendered afterwards at a different resolution or from a
// different mount without capturing it again.
//
// The natives sit in the header's UNK1 namespace under stale names. By hash,
// as the current native databases name them:
//   0xC3AC2FFF9612AC81  START_REPLAY_RECORDING(int mode)   header: _SET_RECORDING_MODE
//   0x071A5197D6AFC8B3  STOP_REPLAY_RECORDING()            header: _STOP_RECORDING_AND_SAVE_CLIP
//   0x88BB3507ED41A240  CANCEL_REPLAY_RECORDING()          header: _STOP_RECORDING_AND_DISCARD_CLIP
//   0x644546EC5287471B  SAVE_REPLAY_RECORDING() -> BOOL    header: _0x644546EC5287471B
//   0x1897CA71995A90B4  IS_REPLAY_RECORDING()
//
// ★ Measured on 1.0.3889.0, and the two modes behave differently:
//   mode 0 = MANUAL (the F1 recording). START records; SAVE_REPLAY_RECORDING
//            writes the clip (returns 1) and, called in the same tick as STOP,
//            stops it too. STOP on its own DROPS the buffer -- a real discard.
//            STOP-then-SAVE on separate ticks saves nothing: that ordering is
//            why "mode 0 does not work" was believed for a while.
//   mode 1 = ACTION REPLAY. STOP saves, CANCEL saves too, and the recorder
//            flushes 30 s (of rendered time) segments, so a 15 s clip captured
//            at 1/3 speed comes out as two files.
// Mode 0 is used. Files land in Documents\Rockstar Games\GTA V\videos\clips\
// as <Mon>-<DD>-<YYYY>-Clip-NNNN.clip (+ .jpg thumbnail), named by the game.
// ⚠⚠ Whatever the mode, the recorder never saves again in a process once it
// has seen SET_GAME_PAUSED -- "Clips must be at least 3 seconds long",
// regardless of length. See m_pauseForCapture in DataExport: with recording
// on, the capture cycle freezes time with SET_TIME_SCALE(0) alone.
void Scenario::setClipRecording(const std::string& action, int mode, int control, int group, int frames) {
	log("Scenario::setClipRecording " + action + " mode=" + std::to_string(mode) +
	    (control >= 0 ? " control=" + std::to_string(control) + " group=" + std::to_string(group) : ""));
	if (action == "start") {
		UNK1::_SET_RECORDING_MODE(mode);                       // START_REPLAY_RECORDING
	} else if (action == "save") {
		// Manual mode: STOP and SAVE in the same tick -- stops AND writes.
		if (UNK1::_IS_RECORDING()) {
			UNK1::_STOP_RECORDING_AND_SAVE_CLIP();               // STOP_REPLAY_RECORDING
			const int r = (int)UNK1::_0x644546EC5287471B();     // SAVE_REPLAY_RECORDING
			log("[longtail] clip: stop+save, SAVE_REPLAY_RECORDING returned " + std::to_string(r), true);
		} else log("[longtail] clip: save requested but not recording", true);
	} else if (action == "discard") {
		// Manual mode: STOP alone drops the buffer. (Action-replay mode would
		// save here instead; the client deletes anything that turns up.)
		if (UNK1::_IS_RECORDING()) UNK1::_STOP_RECORDING_AND_SAVE_CLIP();
	} else if (action == "press") {
		// Emulate a key: hold the control for a few frames so just-pressed edges
		// register. Applied from Scenario::run() in every mode.
		m_pendingControl = control;
		m_pendingControlGroup = group;
		m_pendingControlValue = 1.0f;
		m_pendingControlFrames = frames > 0 ? frames : 3;
	} else if (action == "status") {
		log("[longtail] clip: replay initialized=" + std::to_string(UNK1::_0xDF4B952F7D381B95() != 0) +
		    " available=" + std::to_string(UNK1::_0x4282E08174868BE3() != 0) +
		    " space=" + std::to_string(UNK1::_0x33D47E85B476ABCD(TRUE) != 0) +
		    " recording=" + std::to_string(UNK1::_IS_RECORDING() != 0), true);
	} else {
		log("[longtail] clip: unknown action " + action, true);
	}
}


// ============================================================================
// [rockstar] Render mode: capture a Rockstar Editor replay
// ============================================================================
//
// There is no native that plays a given .clip. What exists: ACTIVATE_ROCKSTAR_EDITOR
// (0x49DA8145672B2725) opens the Editor's frontend, and frontend controls can be
// injected per frame, so the client drives the Editor's menus blind, looking at
// the frames it gets back. Once a replay is playing, this mode keeps the capture
// loop alive with no scenario: the camera rides a render TARGET -- the replayed
// ego, found as the closest vehicle to where the original poses.jsonl began --
// with the same seat mount and the user's offset, or, with no target, whatever
// camera the replay is showing.

void Scenario::startRender(const Value& dc) {
	log("Scenario::startRender", true);
	parseDatasetConfig(dc, true);
	m_renderMode = true;
	exporter.setRenderMode(true);
	exporter.setRenderTarget(0);
	m_targetWanted = false;
	m_pendingControl = -1;
	// A cam of our own exists so a target can be followed; until one is locked
	// the replay's camera renders (script cams off).
	exporter.initialize();
	CAM::RENDER_SCRIPT_CAMS(FALSE, FALSE, 0, TRUE, TRUE);
	running = true;
	lastSafetyCheck = std::clock();
}

void Scenario::replayControl(const std::string& action, float a, float b, int frames) {
	log("Scenario::replayControl " + action + " a=" + std::to_string(a) + " b=" + std::to_string(b) +
	    " frames=" + std::to_string(frames), true);
	if (action == "editor") {
		UNK2::_0x49DA8145672B2725();                          // ACTIVATE_ROCKSTAR_EDITOR
	} else if (action == "reset") {
		UNK2::_0x3353D13F09307691();                          // RESET_EDITOR_VALUES
	} else if (action == "fadein") {
		CAM::DO_SCREEN_FADE_IN((int)a);
	} else if (action == "input") {
		// Held for `frames` frames from runRenderMode(): frontend menus read
		// just-pressed edges, so a single-frame set is often missed.
		m_pendingControl = (int)a;
		m_pendingControlGroup = 2;
		m_pendingControlValue = b;
		m_pendingControlFrames = frames > 0 ? frames : 3;
	} else if (action == "scriptcams") {
		CAM::RENDER_SCRIPT_CAMS(a > 0.5f ? TRUE : FALSE, FALSE, 0, TRUE, TRUE);
	} else if (action == "timescale") {
		GAMEPLAY::SET_TIME_SCALE(a);
		exporter.setResumeTimeScale(a);
	} else {
		log("[longtail] replay: unknown action " + action, true);
	}
}

void Scenario::setRenderTarget(float x, float y, float z, float radius) {
	m_targetX = x; m_targetY = y; m_targetZ = z; m_targetRadius = radius;
	m_targetWanted = radius > 0.0f;
	if (!m_targetWanted) { exporter.setRenderTarget(0); CAM::RENDER_SCRIPT_CAMS(FALSE, FALSE, 0, TRUE, TRUE); }
	log("Scenario::setRenderTarget wanted=" + std::to_string(m_targetWanted), true);
}

void Scenario::applyPendingControl() {
	// Injected input, held for the requested number of frames. ⚠ Runs even when
	// no scenario is active, so a key can be emulated on an idle game too.
	if (m_pendingControl >= 0) {
		CONTROLS::_SET_CONTROL_NORMAL(m_pendingControlGroup, m_pendingControl, m_pendingControlValue);
		if (--m_pendingControlFrames <= 0) m_pendingControl = -1;
	}
}

void Scenario::runRenderMode() {
	// Lock onto the replayed ego once a vehicle shows up near the requested point.
	if (m_targetWanted && !exporter.renderTarget()) {
		Vehicle v = VEHICLE::GET_CLOSEST_VEHICLE(m_targetX, m_targetY, m_targetZ, m_targetRadius, 0, 70);
		if (v && ENTITY::DOES_ENTITY_EXIST(v)) {
			exporter.setRenderTarget(v);
			CAM::RENDER_SCRIPT_CAMS(TRUE, FALSE, 0, TRUE, TRUE);
			log("[longtail] render: locked target vehicle " + std::to_string(v), true);
		}
	}
	// A target that vanished (replay ended / scrubbed) is released; the loop
	// falls back to the replay camera rather than a stale handle.
	if (exporter.renderTarget() && !ENTITY::DOES_ENTITY_EXIST(exporter.renderTarget())) {
		exporter.setRenderTarget(0);
		CAM::RENDER_SCRIPT_CAMS(FALSE, FALSE, 0, TRUE, TRUE);
		log("[longtail] render: target vehicle gone", true);
	}
}


// ============================================================================
// [longtail] Hard invariants: never reach a game-over state, never lose the POV
// ============================================================================

// Every frame. This is deliberately NOT part of SurvivalMode -- SurvivalMode
// chooses how much damage the world is allowed to do, this chooses what happens
// afterwards, and the answer is always "nothing that takes the camera away".
//
// The failure modes being blocked, in the order they actually bite:
//   wasted screen / hospital respawn  -> ped invincibility + health top-up
//   busted / arrest                   -> wanted suppression + arrest reset
//   ejection through the windscreen   -> config flag 32 + ragdoll off
//   dragged out of the car by an NPC  -> SET_PED_CAN_BE_DRAGGED_OUT(FALSE)
//   knocked off a bike                -> SET_PED_CAN_BE_KNOCKED_OFF_VEHICLE(1)
//   fade-to-black on any restart      -> the four fade natives + IGNORE_NEXT_RESTART
// and, as a backstop, an active fade is cancelled the frame it is detected.
// [longtail] Frames come from the swapchain backbuffer, so anything drawn on top
// is baked in: minimap, health/armour, cash, wanted stars, the notification feed,
// the vehicle-name caption, subtitles and the reticle. A fixed corner artifact is
// precisely the sort of thing a world model will learn as part of the scene.
//
// ⚠ HIDE_HUD_AND_RADAR_THIS_FRAME is a *_THIS_FRAME native -- it must be called
// every tick. Setting it once does nothing, the same trap as the density
// multipliers. DISPLAY_HUD/DISPLAY_RADAR in setRecording_active are only a
// backstop; this is the call that actually holds.
void Scenario::hideHudThisFrame() {
	if (!exporter.isRecording()) return;
	UI::HIDE_HUD_AND_RADAR_THIS_FRAME();
	UI::CLEAR_ALL_HELP_MESSAGES();

	// ⚠ Do NOT blanket-hide the whole component range. Hiding components 1..22
	// brought the MINIMAP BACK: some ids in that range interact badly with
	// HIDE_HUD_AND_RADAR_THIS_FRAME and effectively re-assert the radar. Hide only
	// the captions that HIDE_HUD_AND_RADAR_THIS_FRAME genuinely misses.
	//   6 VEHICLE_NAME   7 AREA_NAME   8 VEHICLE_CLASS   9 STREET_NAME
	UI::HIDE_HUD_COMPONENT_THIS_FRAME(6);
	UI::HIDE_HUD_COMPONENT_THIS_FRAME(7);
	UI::HIDE_HUD_COMPONENT_THIS_FRAME(8);
	UI::HIDE_HUD_COMPONENT_THIS_FRAME(9);

	// Belt and braces: assert these every frame rather than once when recording
	// starts. GTA re-enables the radar on area transitions and on entering a
	// vehicle, both of which happen constantly during a capture run.
	UI::DISPLAY_RADAR(FALSE);
	UI::DISPLAY_HUD(FALSE);
}

void Scenario::enforceGameOverGuards() {
	if (player == NULL || !ENTITY::DOES_ENTITY_EXIST(ped)) return;

	// --- the ped must survive, unconditionally ---------------------------
	PLAYER::SET_PLAYER_INVINCIBLE(player, TRUE);
	ENTITY::SET_ENTITY_INVINCIBLE(ped, TRUE);
	ENTITY::SET_ENTITY_PROOFS(ped, 1, 1, 1, 1, 1, 1, 1, 1);
	PED::SET_PED_SUFFERS_CRITICAL_HITS(ped, FALSE);
	if (m_egoPedMaxHealth <= 0) m_egoPedMaxHealth = ENTITY::GET_ENTITY_MAX_HEALTH(ped);
	if (m_egoPedMaxHealth > 0 && ENTITY::GET_ENTITY_HEALTH(ped) < m_egoPedMaxHealth)
		ENTITY::SET_ENTITY_HEALTH(ped, m_egoPedMaxHealth);

	// --- the ped must stay in the driver's seat ---------------------------
	// Ejection is not just a lost clip: the camera tracks the vehicle, so an
	// ejected ped leaves a driverless car coasting with a live POV.
	PED::SET_PED_CONFIG_FLAG(ped, 32, FALSE);   // will NOT fly through windscreen
	PED::SET_PED_CAN_RAGDOLL(ped, FALSE);
	PED::SET_PED_CAN_BE_DRAGGED_OUT(ped, FALSE);
	PED::SET_PED_CAN_BE_KNOCKED_OFF_VEHICLE(ped, 1);  // 1 = never

	// --- no police, no arrest --------------------------------------------
	PLAYER::SET_POLICE_IGNORE_PLAYER(player, TRUE);
	PLAYER::SET_MAX_WANTED_LEVEL(0);
	PLAYER::SET_WANTED_LEVEL_MULTIPLIER(0.0f);
	PLAYER::CLEAR_PLAYER_WANTED_LEVEL(player);
	PED::SET_CREATE_RANDOM_COPS(FALSE);
	if (PLAYER::IS_PLAYER_BEING_ARRESTED(player, TRUE)
			|| PLAYER::IS_PLAYER_BEING_ARRESTED(player, FALSE)) {
		PLAYER::RESET_PLAYER_ARREST_STATE(player);
		m_gameOverInterventions++;
	}

	// --- no restart, no fade ----------------------------------------------
	GAMEPLAY::SET_FADE_OUT_AFTER_DEATH(FALSE);
	GAMEPLAY::SET_FADE_OUT_AFTER_ARREST(FALSE);
	GAMEPLAY::SET_FADE_IN_AFTER_DEATH_ARREST(FALSE);
	GAMEPLAY::SET_FADE_IN_AFTER_LOAD(FALSE);
	GAMEPLAY::IGNORE_NEXT_RESTART(TRUE);
	for (int i = 0; i < 6; i++) GAMEPLAY::DISABLE_HOSPITAL_RESTART(i, TRUE);
	for (int i = 0; i < 6; i++) GAMEPLAY::DISABLE_POLICE_RESTART(i, TRUE);

	// Backstop: if anything still managed to start a fade, cancel it the frame
	// we see it. A black frame in the middle of a clip is unusable data.
	if (CAM::IS_SCREEN_FADED_OUT() || CAM::IS_SCREEN_FADING_OUT()) {
		CAM::DO_SCREEN_FADE_IN(0);
		m_gameOverInterventions++;
	}
}

// ⚠ All five are *_THIS_FRAME natives and must be re-issued every tick.
void Scenario::applyDensityThisFrame() {
	if (m_suppressAmbient) {
		// Deterministic-population mode: the game spawns nothing; every actor in
		// the scene was placed by seedScene from the scene seed.
		VEHICLE::SET_VEHICLE_DENSITY_MULTIPLIER_THIS_FRAME(0.0f);
		VEHICLE::SET_RANDOM_VEHICLE_DENSITY_MULTIPLIER_THIS_FRAME(0.0f);
		VEHICLE::SET_PARKED_VEHICLE_DENSITY_MULTIPLIER_THIS_FRAME(0.0f);
		PED::SET_PED_DENSITY_MULTIPLIER_THIS_FRAME(0.0f);
		PED::SET_SCENARIO_PED_DENSITY_MULTIPLIER_THIS_FRAME(0.0f, 0.0f);
		return;
	}
	VEHICLE::SET_VEHICLE_DENSITY_MULTIPLIER_THIS_FRAME(m_density.vehicle);
	VEHICLE::SET_RANDOM_VEHICLE_DENSITY_MULTIPLIER_THIS_FRAME(m_density.randomVehicle);
	VEHICLE::SET_PARKED_VEHICLE_DENSITY_MULTIPLIER_THIS_FRAME(m_density.parkedVehicle);
	PED::SET_PED_DENSITY_MULTIPLIER_THIS_FRAME(m_density.ped);
	PED::SET_SCENARIO_PED_DENSITY_MULTIPLIER_THIS_FRAME(m_density.scenarioPed, m_density.scenarioPed);
}

// Keep the POV welded to the front of the ego vehicle. The camera is positioned
// from the vehicle matrix every capture (DataExport::setRenderingCam), so the
// invariant reduces to: the vehicle exists, the ped is driving it, and the
// scripted camera is alive and rendering.
void Scenario::enforceEgoIntegrity() {
	if (!ENTITY::DOES_ENTITY_EXIST(m_ownVehicle)) return;   // clip is over; client aborts

	// Re-seat a ped that left the car for any reason.
	if (ENTITY::DOES_ENTITY_EXIST(ped) && !PED::IS_PED_IN_VEHICLE(ped, m_ownVehicle, TRUE)) {
		PED::SET_PED_INTO_VEHICLE(ped, m_ownVehicle, -1);
		m_gameOverInterventions++;
	}

	// The scripted cam can be destroyed by a cutscene or a respawn path. Rebuild
	// it rather than silently falling back to the gameplay camera, which is not
	// where our extrinsics say it is.
	if (!CAM::DOES_CAM_EXIST(exporter.getCamera())) {
		exporter.initialize();
		m_gameOverInterventions++;
	}
	CAM::SET_CAM_ACTIVE(exporter.getCamera(), TRUE);
	CAM::RENDER_SCRIPT_CAMS(TRUE, FALSE, 0, TRUE, TRUE);
}

void Scenario::setActorBehaviour(int drivingStyle, float aggressiveness, float ability,
                                 float cruiseSpeed, bool steersAround, bool trafficAggression,
                                 bool pedInteraction, float pedCrossChance) {
	m_npcDrivingStyle   = drivingStyle;
	m_npcAggressiveness = aggressiveness;
	m_npcAbility        = ability;
	m_npcCruiseSpeed    = cruiseSpeed;
	m_npcSteersAround   = steersAround;
	m_trafficAggression = trafficAggression;
	m_pedInteraction    = pedInteraction;
	m_pedCrossChance    = pedCrossChance;
	// ⚠ Drivers already re-tasked under the OLD behaviour keep it until the sweep
	// visits them again; the sweep applies style/aggression every pass but only
	// re-tasks once per vehicle. Clearing the set makes the next pass re-task
	// everyone under the new behaviour, so a variation change is not gradual.
	m_retasked.clear();
	log("[actors] style=" + std::to_string(m_npcDrivingStyle) +
	    " aggr=" + std::to_string(m_npcAggressiveness) +
	    " ability=" + std::to_string(m_npcAbility) +
	    " cruise=" + std::to_string(m_npcCruiseSpeed) +
	    " steersAround=" + std::to_string((int)m_npcSteersAround) +
	    " trafficAggression=" + std::to_string((int)m_trafficAggression) +
	    " pedCross=" + std::to_string(m_pedCrossChance));
}

void Scenario::setSceneDensity(float vehicle, float randomVehicle, float parkedVehicle,
                               float ped_, float scenarioPed) {
	m_density.vehicle = vehicle;
	m_density.randomVehicle = randomVehicle;
	m_density.parkedVehicle = parkedVehicle;
	m_density.ped = ped_;
	m_density.scenarioPed = scenarioPed;
}

// Capture already pauses the game and sets time scale 0 per frame, restoring it
// afterwards. Upstream restored a hard-coded 1.0f; making that a variable turns
// slow-motion capture into a free knob, which matters because at 30 m/s the most
// informative half-second of a collision otherwise gets the fewest frames.
void Scenario::setTimeScale(float scale) {
	exporter.setResumeTimeScale(scale);
}


// [longtail] Kick off streaming for a location we have not travelled to yet.
// Call this during clip N for clip N+1: NEW_LOAD_SCENE_START is asynchronous, so
// the world around the next spawn point streams in while the current clip is
// still recording, and the relocate that follows pays almost nothing.
void Scenario::prepareLocation(float x, float y, float z) {
	Vector3 pos; float heading;
	PATHFIND::GET_CLOSEST_VEHICLE_NODE_WITH_HEADING(x, y, z, &pos, &heading, 0, 0, 0);
	STREAMING::REQUEST_COLLISION_AT_COORD(pos.x, pos.y, pos.z);
	STREAMING::REQUEST_ADDITIONAL_COLLISION_AT_COORD(pos.x, pos.y, pos.z);
	// Non-blocking. Stopped and restarted per request; the game tolerates this.
	STREAMING::NEW_LOAD_SCENE_STOP();
	STREAMING::NEW_LOAD_SCENE_START(pos.x, pos.y, pos.z, 0.0f, 0.0f, 1.0f, 250.0f, 0);
}
