
#include <stdlib.h>
#include <ctime>
// [longtail] std headers formerly arriving via ObjectDetection.h -> opencv
#include <vector>
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




using namespace rapidjson;


/*
	The DataExport Class is used to aggregate all the data that will be sent over a TCP-Connection in a JSON Document. 
	To do this the data is generated (mostly from GTAV functions or in ObjectDet
*/
class DataExport {
private:

	// TODO why is this a unique pointer? this should be an object, then the -> should be replaced with .
#ifndef LONGTAIL_SLIM
	std::unique_ptr<ObjectDetection> m_pObjDet = NULL;
#endif // LONGTAIL_SLIM
	Rewarder* rewarder;

	bool direction;
	bool reward;
	bool throttle;
	bool brake;
	bool steering;
	bool speed;
	bool yawRate;
	bool location;
	bool time;
	bool exportBBox2D;
	bool exportBBox2DUnprocessed;
	bool occlusionImage;
	bool unusedStencilIPixelmage;
	bool segmentationImage;
	bool instanceSegmentationImage;
	bool instanceSegmentationImageColor;
	bool exportLiDAR;
	bool exportLiDARRaycast;
	float maxLidarDist;
	bool export2DPointmap;
	bool exportSome2DPointmapText;
	bool exportLiDARDepthStats;
	bool exportStencliBuffer;
	bool exportStencilImage;
	bool exportIndividualStencilImages;
	bool exportDepthBuffer;


	Document d;

	//void setDirection();
	void exportReward();
	void exportCameraPosition();
	void exportCameraAngle();
	void exportThrottle();
	void exportBrake();
	void exportSteering();
	void exportSpeed();
	void exportYawRate();
	void exportLocation();
	void exportTime();
	void exportHeightAboveGround();

	// [longtail] camera intrinsics + engine clock, emitted every frame
	void exportCameraIntrinsics();
	void exportGameTime();
	// [longtail] outcome detection + road context
	void exportEgoState();
	// Camera mount as a fraction of the vehicle's half-extent. 0.35 forward is a
	// windscreen position; 0.90 (the old value) is on the front bumper, where a
	// head-on pedestrian impact happens out of frame.
	// Offsets from the driver's seat bone, in absolute metres -- model-independent,
	// which is the whole point of anchoring to the seat rather than to a fraction
	// of the bounding box.
	//
	// ⚠ Deliberately FORWARD OF and ABOVE the driver, not at the driver: a camera
	// at eye position sees the dashboard, the wheel and the A-pillars, which is the
	// "inside the car" complaint, and on a long-bonnet model the bonnet still filled
	// a third of the frame (measured: buffalo 36%, asea 20%). Sitting just outside
	// the windscreen and near roof height clears the interior entirely and leaves a
	// sliver of bonnet for scale.
	float m_seatForward      = 0.90f;
	// ⚠ 0.38 put the camera at DASHBOARD height (measured: 5 cm above vehicle
	// centre on a sultan), where the bonnet fills the lower frame again. A
	// driver's eye sits ~0.65 m above the seat base.
	float m_seatUp           = 0.95f;
	// Fallback only, for a model with no usable seat bone.
	unsigned m_scenarioGen = 0;
	float m_mountForwardFrac = 0.35f;
	// Windscreen height, not roof: from the roof the bonnet and the road just
	// in front of the bumper are both below frame, which is where a struck
	// pedestrian ends up.
	float m_mountUpFrac      = 0.62f;
public:
	void setCameraMountFractions(float fwd, float up) {
		if (fwd > -1.0f && fwd < 1.5f) m_mountForwardFrac = fwd;
		if (up > 0.0f && up < 2.0f) m_mountUpFrac = up;
		m_autoMountModel = 0;   // force recompute on the next frame
	}
	// Pushed in by Scenario on every build; DataExport is owned by Scenario and has
	// no pointer back, so this mirrors how the camera mount is handed over.
	void setScenarioGen(unsigned g) { m_scenarioGen = g; }
	void setSeatOffsets(float fwd, float up) {
		if (fwd > -1.5f && fwd < 2.5f) m_seatForward = fwd;
		if (up > -1.0f && up < 2.0f) m_seatUp = up;
		m_autoMountModel = 0;
	}
private:
	void exportTrafficReaction();
	void exportRoadContext();


	void setRenderingCam(Vehicle v);
	void capture();


	bool recording_active = false;

	// [longtail] Time scale restored after each captured frame. Capture pauses the
	// game and sets scale 0; upstream restored a hard-coded 1.0f. Lower this around
	// a collision to spend more frames on the highest-information moment.
	float m_resumeTimeScale = 1.0f;
	// [longtail] Whether the capture cycle wraps itself in SET_GAME_PAUSED. ⚠ The
	// Rockstar Editor's replay recorder latches "paused" on that native and never
	// records again in the process, so a .clip-recording run must capture with
	// SET_TIME_SCALE(0) alone. The JSON build and the frame grab already happen
	// inside one script tick, so the world cannot advance between them either way.
	bool  m_pauseForCapture = true;
	// [rockstar] Frames off: export poses at the requested rate but never touch
	// the backbuffer and never freeze time for a grab. ★ With no image there is
	// nothing for the pose to be consistent WITH, so the freeze has no purpose --
	// and it is the freeze that stretches a 15 s clip into ~45 s of rendering,
	// which the Editor's recorder chops into 30 s segments. Poses + .clip only:
	// game time runs at wall time, one .clip per clip, ~3x the capture rate.
	bool  m_captureFrames = true;
	// [rockstar] Render mode: capture a Rockstar Editor replay instead of a live
	// scenario. There is no scenario ego then; the camera is mounted on a render
	// TARGET (the replayed ego, located by the client from the original poses) or,
	// with no target, left where the replay puts it and its pose exported instead.
	bool    m_renderMode   = false;
	Vehicle m_renderTarget = 0;


	Cam camera = NULL;

	// Persistent Camera offsets
	Vector3 cameraPositionOffset = { 0, 0, 0 };
	// [longtail] Mount derived from the ego's own bounding box, so one config works
	// across a Blista and a Packer. cameraPositionOffset is a delta on top of it.
	Hash m_autoMountModel = 0;
	Vector3 m_autoMount = { 0, 0, 0 };
	Vector3 autoMountFor(Vehicle v);
	Vector3 cameraRotationOffset = { 0, 0, 0 };

	int instance_index = 0;



public:
	StringBuffer generateMessage();
	void parseDatasetConfig(const Value& dc, bool setDefaults);
	void buildJSONObject();

	void setRecording_active(bool x);
	bool isRecording() const { return recording_active; }

	// [longtail] Scenario's per-frame POV-integrity guard needs to see the
	// scripted cam, and the slow-motion knob writes the resume time scale.
	// Accessors rather than making the fields public: ownership stays here.
	Cam getCamera() const { return camera; }
	void setResumeTimeScale(float s) { m_resumeTimeScale = s; }
	void setPauseForCapture(bool b) { m_pauseForCapture = b; }
	bool captureFrames() const { return m_captureFrames; }
	void setRenderMode(bool on) { m_renderMode = on; }
	bool renderMode() const { return m_renderMode; }
	void setRenderTarget(Vehicle v) { m_renderTarget = v; }
	Vehicle renderTarget() const { return m_renderTarget; }
	//: The vehicle the camera and the ego exports refer to: the render target in
	//: render mode, else the scenario's ego, else 0. Every exporter that used to
	//: dereference m_ownVehicle blindly goes through this.
	Vehicle egoHandle() const {
		if (m_renderTarget && ENTITY::DOES_ENTITY_EXIST(m_renderTarget)) return m_renderTarget;
		if (m_ownVehicle && *m_ownVehicle && ENTITY::DOES_ENTITY_EXIST(*m_ownVehicle)) return *m_ownVehicle;
		return 0;
	}
	void freezeFrameNoCam();
	bool pauseForCapture() const { return m_pauseForCapture; }
	float getResumeTimeScale() const { return m_resumeTimeScale; }

	// TODO make private, make camera fully owned by DataExport
	//Cam * camera;
	//Vector3 * cameraPositionOffset;
	//Vector3 * cameraRotationOffset;
	Vehicle * m_ownVehicle;


	//void generateSecondaryPerspectives();
	//void generateSecondaryPerspective(ObjEntity vInfo);


	// TODO make private, after having camera fully in DataExport ownership
	void setCamParams();

	// TODO move to private
	// ⚠ Must be initialised. It was a raw uninitialised pointer, which was
	// harmless only while the code unconditionally did `new ScreenCapturer(...)`
	// every time. The moment anything READS it first -- e.g. a reuse check --
	// it dereferences garbage and takes the whole game down.
	ScreenCapturer* screenCapturer = NULL;

	void initialize();

	//TODO move to private
	void setCameraPositionAndRotation(float x, float y, float z, float rot_x, float rot_y, float rot_z);



};