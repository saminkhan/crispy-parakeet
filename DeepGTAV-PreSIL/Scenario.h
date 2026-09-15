#pragma once

#include <stdlib.h>
#include <ctime>
// [longtail] std headers formerly arriving via ObjectDetection.h -> opencv
#include <vector>
#include <set>
#include <string>
#include <memory>
#include <unordered_map>
#include <algorithm>


#include "lib/script.h"
#include "lib/utils.h"

#include "lib/rapidjson/document.h"
#include "lib/rapidjson/stringbuffer.h"

#include "ScreenCapturer.h"
#include "Rewarders\Rewarder.h"
#ifndef LONGTAIL_SLIM
#include "LiDAR.h"
#endif // LONGTAIL_SLIM
#include "Functions.h"
#include "CamParams.h"
#include <memory>
#ifndef LONGTAIL_SLIM
#include "ObjectDetection.h"
#endif // LONGTAIL_SLIM
#include "Constants.h"

#include "DataExport.h"

#include <time.h>

using namespace rapidjson;

//#define DEBUG 1

struct SpawnedPed {
	Ped ped;
	time_t spawntime;
	int actorId = -1;   // [longtail] client-assigned handle, -1 = untracked
};

struct SpawnedVehicle {
	Vehicle vehicle;
	time_t spawntime;
	int actorId = -1;   // [longtail] client-assigned handle, -1 = untracked
};

// [longtail] Ambient density. ⚠ The underlying natives are all *_THIS_FRAME, so
// these must be re-applied every tick from Scenario::run(); a one-shot setter
// silently does nothing.
struct SceneDensity {
	float vehicle = 1.0f;
	float randomVehicle = 1.0f;
	float parkedVehicle = 1.0f;
	float ped = 1.0f;
	float scenarioPed = 1.0f;
};

// [longtail] Which of the upstream "keep the run alive" guards are active.
// Upstream applies ALL of these unconditionally every 10s, which makes
// collisions, damage, fire and reactive NPC behaviour impossible to capture.
struct SurvivalMode {
	bool policeIgnore      = true;   // no wanted level / no chase
	bool everyoneIgnore    = true;   // ** set false to get REACTIVE traffic **
	bool playerInvincible  = true;   // ped never dies (keep true: death = cutscene)
	bool vehicleInvincible = true;   // ** set false to get crashes/damage/fire **
	bool seatbelt          = true;   // keep true: ped ejection breaks the ego loop
	float aggressiveness   = 0.0f;
	float ability          = 100.0f;
};


class Scenario {
private:
	static char* weatherList[14];
	static char* vehicleList[3];


	Vehicle m_ownVehicle = NULL;
	Player player = NULL;
	Ped ped = NULL;
	Vector3 dir;

	float x, y, z;
    float startHeading;
	int hour, minute;
	const char* _weather;
	const char* _vehicle;

    bool offscreen;
    bool showBoxes;
    bool stationaryScene;

	float currentThrottle = 0.0;
	float currentBrake = 0.0;
	float currentSteering = 0.0;


	std::clock_t lastSafetyCheck;
	int _drivingMode;
	float _setSpeed;

	SurvivalMode m_survival;                       // [longtail]
	int m_nextActorId = 0;                         // [longtail]
	SceneDensity m_density;                        // [longtail]
	int m_egoPedMaxHealth = 0;                     // [longtail]
	int m_gameOverInterventions = 0;               // [longtail] diagnostics

	bool running = false;

    int m_startArea = 1; //Downtown (see s_locationBounds)
    std::vector<std::vector<char>> m_polyGrid;


    bool vehicles_created = false;
    std::vector<VehicleToCreate> vehiclesToCreate;
    std::vector<PedToCreate> pedsToCreate;

	std::vector<SpawnedPed> spawnedPeds;
	std::vector<SpawnedVehicle> spawnedVehicles;
	double spawnedEntitiesDespawnSeconds;


public:
	float rate;

	void start(const Value& sc, const Value& dc);
	void stop();
	void config(const Value& sc, const Value& dc);
	void setCommands(float throttle, float brake, float steering);
	void run();

	// TODO remove
	StringBuffer generateMessage();
	void setRecording_active(bool x);
	
	void goToLocation(float x, float y, float z, float setSpeed);
	void teleportToLocation(float x, float y, float z);

	// TODO remove
	void setCameraPositionAndRotation(float x, float y, float z, float rot_x, float rot_y, float rot_z);


    //Tracking variables
    bool collectTracking;
    //# of instances in one series
    const int trSeriesLength = 500;
    //# of seconds between series
    const int trSeriesGapTime = 30;
    //Used for keeing track of when to add the gap
    bool trSeriesGap = false;


	void createVehicle(const char* model, float relativeForward, float relativeRight, float heading, int color, int color2, bool placeOnGround, bool withLifeJacketPed,
	                   float speed = 2.0f, int drivingMode = 16777216, int actorId = -1);   // [longtail] speed/drivingMode/actorId

	// [longtail] runtime control of the long-tail behaviour
	void setSurvivalMode(bool policeIgnore, bool everyoneIgnore, bool playerInvincible,
	                     bool vehicleInvincible, bool seatbelt, float aggressiveness, float ability);
	void applySurvivalSettings();
	void taskVehicleTempAction(int actorId, int action, int durationMs);
	void taskVehicleDriveToCoord(int actorId, float x, float y, float z, float speed, int drivingMode);
	void setEgoDrivingMode(int drivingMode, float setSpeed);
	void setClipRecording(const std::string& action, int mode, int control, int group, int frames);
	void applyPendingControl();
	int  m_pendingControlGroup = 2;
	void setCapturePause(bool enabled) { exporter.setPauseForCapture(enabled); }
	// [rockstar] Replay capture ("render mode") -- see Scenario.cpp.
	void startRender(const Value& dc);
	void replayControl(const std::string& action, float a, float b, int frames);
	void setRenderTarget(float x, float y, float z, float radius);
	bool  m_renderMode = false;
	int   m_pendingControl = -1;     // frontend control id being held, or -1
	float m_pendingControlValue = 0.0f;
	int   m_pendingControlFrames = 0;
	float m_targetX = 0, m_targetY = 0, m_targetZ = 0, m_targetRadius = 0;
	bool  m_targetWanted = false;
	void  runRenderMode();
	bool resolveActor(int actorId, Vehicle& vehOut, Ped& pedOut);

	// [longtail] ambient density + capture time scale
	void setSceneDensity(float vehicle, float randomVehicle, float parkedVehicle,
	                     float ped, float scenarioPed);
	void setTimeScale(float scale);

	// [longtail] Hard invariants, enforced EVERY frame regardless of survival mode.
	// These are not knobs: the generator must never enter a game-over state and
	// the POV camera must never leave the front of the ego vehicle.
	void hideHudThisFrame();
	void makeTrafficAggressive();
	bool  m_trafficAggression = true;
	float m_npcAggressiveness = 1.0f;   // 0..1, stock traffic is ~0.0
	float m_npcAbility        = 0.0f;   // 0..1, stock traffic is ~1.0
	// ★ Was 1074528805, copied from a community table and labelled "rushed:
	// ignores lights/lanes". Decomposed, it is:
	//   STOP_BEFORE_VEHICLES | AVOID_VEHICLES | AVOID_OBJECTS
	//   | ALLOW_WRONG_WAY | TAKE_SHORTEST_PATH
	// ⚠ The first two are RESTRAINT bits. The "aggressive" preset was telling every
	// NPC driver to stop before and avoid other vehicles, which is why traffic
	// would not commit to contact no matter what else was tuned. A wrong bit here
	// does not error -- it quietly produces a boring clip, which is exactly the
	// warning drivingstyles.py carries and that this number ignored.
	//
	// Built from named bits now: wrong-way + shortest-path, no avoidance, no
	// stopping. 512 | 262144.
	int   m_npcDrivingStyle   = 512 | 262144;
	float m_npcCruiseSpeed    = 40.0f;  // m/s ceiling for NPC drivers
	float m_npcTankHealth     = 250.0f; // start tanks weakened so impacts can ignite
	int   m_aggroTick         = 0;
	// Vehicles already re-tasked this scenario, so the sweep refreshes flags every
	// pass but does not reset a drive task it already issued.
	std::set<int> m_retasked;

	// [longtail] ★ Scene seeding. Measured over 13 clips: damage to other vehicles
	// scales ~4.7x with how many are in scene (mean 0.7 damaged when <=4 nearby,
	// 3.3 when >=8), and the MEDIAN clip had only 4 vehicles within 60 m. Density
	// was the binding constraint on chaos, not driver aggression.
	//
	// ⚠ SET_VEHICLE_DENSITY_MULTIPLIER_THIS_FRAME cannot fix that: it SCALES GTA's
	// existing population model rather than inventing traffic, so 3x of a near-zero
	// base population is still near zero. The only way to guarantee a population is
	// to place it.
	// seed: every model choice in the seeded population derives from it, so the
	// same scene gets the same people and the same cars. clearAmbient: remove the
	// game's own population first and keep its spawner at zero for the clip, so the
	// seeded population IS the population -- and is therefore reproducible.
	void  seedScene(int vehicles, int peds, int cyclists, float radius,
	                unsigned seed, bool clearAmbient);
	Ped   createSeededPed(float x, float y, float z, float heading, unsigned idx);
	unsigned m_seedValue      = 0;
	bool  m_suppressAmbient   = false;
	// ★ Bumped on every scenario build. The client's warmup waits for it to ADVANCE
	// after sending Config, which is the only exact way to know a relocate has
	// happened. Proximity cannot tell: for variations 2..N the ego is already at
	// the scene, so "within N m of the target" was true before the relocate ran and
	// warmup released early -- the relocate then landed inside the clip.
	unsigned m_scenarioGen    = 0;
	unsigned scenarioGen() const { return m_scenarioGen; }
	Ped   placeVRU(int idx, bool crossing);
	void  setVRUTarget(int n) { m_vruTarget = n; }
	int   m_seedVehicles      = 0;
	int   m_seedPeds          = 0;
	int   m_seedCyclists      = 0;
	float m_seedRadius        = 120.0f;
	bool  m_seedPending       = false;   // run once, after the ego is settled
	// [longtail] ★ Keeping VRUs in front of the ego, not just placing them once.
	// At 25 m/s the car covers the whole 15-70 m seeding cone in under 3 s, so a
	// one-shot placement leaves the rest of the clip empty -- observed directly:
	// a clip seeded with 18 pedestrians showed none in any sampled frame. Top up
	// the forward cone as the ego outruns it.
	void  topUpVRUs();
	int   m_vruTarget         = 0;      // peds wanted in the cone at any moment
	float m_vruNear           = 12.0f;  // cone: metres ahead, near edge
	float m_vruFar            = 45.0f;  // ...far edge; beyond this they are specks
	float m_vruHalfWidth      = 14.0f;  // ...lateral half-width
	int   m_vruTick           = 0;
	int   m_vruSpawnCursor    = 0;
	// Re-task the ego when it stops for no reason. See the note on the definition:
	// measured stalls had frames flowing normally while speed sat at zero, which
	// means the drive task ended, not that the plugin blocked.
	void  enforceEgoMobility();
	bool  m_egoMobility       = true;
	float m_egoStuckSeconds   = 2.5f;
	std::clock_t m_lastMovingClock = 0;
	bool  m_unstuckIssued     = false;
	int   m_unstuckCount      = 0;
	void  igniteWrecks();
	bool  m_igniteWrecks      = true;
	float m_igniteBelowHealth = 400.0f;  // body health under which a wreck ignites
	// The ego is ignitable too, or the `fire` outcome label -- which reads the
	// EGO's on-fire state -- can never be produced. The player ped is invincible,
	// so the car burns without a death cutscene.
	bool  m_igniteEgo         = true;
	std::set<int> m_ignited;
	void  setIgniteWrecks(bool enabled, float belowHealth) {
		m_igniteWrecks = enabled;
		if (belowHealth > 0.0f) m_igniteBelowHealth = belowHealth;
		log("[fire] igniteWrecks=" + std::to_string((int)m_igniteWrecks) +
		    " belowHealth=" + std::to_string(m_igniteBelowHealth));
	}
	void  makePedsInteractive();
	bool  m_pedInteraction    = true;
	float m_pedCrossChance    = 0.35f;  // fraction of eligible peds sent across

	// [longtail] Road leash. The ego is an AI wander task, and a wander task with
	// IGNORE_ROADS (or a knock from a collision) will happily drive up an
	// embankment and stay there -- which produces a 15 s clip of dirt. The leash
	// is enforced in the plugin rather than only client-side, because the client
	// cannot see the ego leave the network until the frame has already shipped.
	void  enforceRoadLeash();
	bool  m_roadLeash         = true;
	float m_roadLeashDist     = 18.0f;  // m from nearest vehicle node before we act
	float m_roadLeashSeconds  = 1.5f;   // sustained, so a legal shortcut is allowed
	int   m_leashTick         = 0;
	int   m_leashStrikes      = 0;      // consecutive off-network checks
	bool  m_leashActive       = false;  // currently steering back to the network
	int   m_leashRecoveries   = 0;      // telemetry: how often we had to intervene
	float m_lastRoadDist      = 0.0f;
	int   egoDrivingModeOnRoad() const;
	void  setRoadLeash(bool enabled, float dist, float seconds);
	// [longtail] ★ Actor behaviour as a single settable unit. Until now every NPC
	// knob -- driving style, aggressiveness, ability, cruise speed, whether drivers
	// steer around things, whether peds cross -- was a private member with no
	// message, so the plugin could only ever run one flavour of traffic. The
	// counterfactual capture needs the SAME scene replayed with actors sane in one
	// clip and reckless in the next, which means these have to come from the client
	// per clip, not from the build.
	void  setActorBehaviour(int drivingStyle, float aggressiveness, float ability,
	                        float cruiseSpeed, bool steersAround, bool trafficAggression,
	                        bool pedInteraction, float pedCrossChance);
	bool  m_npcSteersAround   = false;  // TRUE = stock avoidance, FALSE = commits to contact
	void  setCameraMountFractions(float fwd, float up) { exporter.setCameraMountFractions(fwd, up); }
	int   leashRecoveries() const { return m_leashRecoveries; }

	void enforceGameOverGuards();
	void applyDensityThisFrame();
	void enforceEgoIntegrity();

	// [longtail] Warmup elimination. Streaming for the NEXT clip is kicked off
	// during the CURRENT one, so the relocate does not pay for it.
	void prepareLocation(float x, float y, float z);
	int gameOverInterventions() const { return m_gameOverInterventions; }
	void createPed(const char* model, float relativeForward, float relativeRight, float relativeUp, float heading, bool placeOnGround, const char* animDict, const char* animName, int task = 0);
	
	void setWeather(const char * weather);
	void setClockTime(int hour, int minute, int second);

	//TODO move to private
	DataExport exporter;


private:
	void parseScenarioConfig(const Value& sc, bool setDefaults);
	void parseDatasetConfig(const Value& dc, bool setDefaults);
	void buildScenario();

    void drawBoxes(Vector3 BLL, Vector3 FUR, Vector3 dim, Vector3 upVector, Vector3 rightVector, Vector3 forwardVector, Vector3 position, int colour);
    void createVehicles();
    //void setPosition();

	void despawnSpawnedObjectsAfterTime();


};