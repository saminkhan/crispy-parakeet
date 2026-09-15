#include "DataExport.h"

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





const float VERT_CAM_FOV = 59; //In degrees




void DataExport::initialize() {
	Vector3 rotation;

	{
		Vehicle ev = egoHandle();
		if (ev) rotation = ENTITY::GET_ENTITY_ROTATION(ev, 0);
		else { rotation.x = 0.0f; rotation.y = 0.0f; rotation.z = 0.0f; }   // render mode, no target yet
	}
	CAM::DESTROY_ALL_CAMS(TRUE);
	camera = CAM::CREATE_CAM("DEFAULT_SCRIPTED_CAMERA", TRUE);
	//if (strcmp(_vehicle, "packer") == 0) CAM::ATTACH_CAM_TO_ENTITY(camera, vehicle, 0, 2.35, 1.7, TRUE);
	//else CAM::ATTACH_CAM_TO_ENTITY(camera, vehicle, 0, CAM_OFFSET_FORWARD, CAM_OFFSET_UP, TRUE);
	CAM::SET_CAM_FOV(camera, VERT_CAM_FOV);
	CAM::SET_CAM_ACTIVE(camera, TRUE);


	// TODO CANGED
	CAM::SET_CAM_ROT(camera, rotation.x, rotation.y, rotation.z, 0);
	//CAM::SET_CAM_ROT(camera, rotation.x, rotation.y, 2, 0);

	// pos = CAM::GET_CAM_COORD()
	//Vector3 camPos;
	//camPos = CAM::GET_CAM_COORD(camera);
	//CAM::SET_CAM_COORD(camera, camPos.x + 40.0, camPos.y + 40.0, camPos.z + 40.0);


	CAM::SET_CAM_INHERIT_ROLL_VEHICLE(camera, TRUE);


}


//void DataExport::setCapturedData() {
//
//}







void DataExport::parseDatasetConfig(const Value& dc, bool setDefaults) {
	log("DataExport::parseDatasetConifg");


	if (dc.HasMember("captureFrames") && dc["captureFrames"].IsBool()) {
		m_captureFrames = dc["captureFrames"].GetBool();
		log(std::string("[longtail] captureFrames=") + (m_captureFrames ? "true" : "false"), true);
	} else if (setDefaults) m_captureFrames = true;
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

	if (!dc["screenResolution"].IsNull()) {
		if (!dc["screenResolution"][0].IsNull()) s_camParams.screenWidth = dc["screenResolution"][0].GetInt();
		else if (setDefaults) s_camParams.screenWidth = _DEFAULT_SCREEN_WIDHT_;

		if (!dc["screenResolution"][1].IsNull()) s_camParams.screenHeight = dc["screenResolution"][1].GetInt();
		else if (setDefaults) s_camParams.screenHeight = _DEFAULT_SCREEN_HEIGHT_;
	}
	else if (setDefaults){
		s_camParams.screenWidth = _DEFAULT_SCREEN_WIDHT_;
		s_camParams.screenHeight = _DEFAULT_SCREEN_HEIGHT_;
	}

	//Need to reset camera params when dataset config is received
	s_camParams.init = false;

	if (dc["reward"].IsArray()) {
		if (dc["reward"][0].IsFloat() && dc["reward"][1].IsFloat()) {
			rewarder = new GeneralRewarder((char*)(GetCurrentModulePath() + "paths.xml").c_str(), dc["reward"][0].GetFloat(), dc["reward"][1].GetFloat());
			reward = true;
		}
		else if (setDefaults) reward = _REWARD_;
	}
	else if (setDefaults) reward = _REWARD_;


	if (!dc["startIndex"].IsNull()) {
		instance_index = dc["startIndex"].GetInt();
	}
	if (!dc["throttle"].IsNull()) throttle = dc["throttle"].GetBool();
	else if (setDefaults) throttle = _THROTTLE_;
	if (!dc["brake"].IsNull()) brake = dc["brake"].GetBool();
	else if (setDefaults) brake = _BRAKE_;
	if (!dc["steering"].IsNull()) steering = dc["steering"].GetBool();
	else if (setDefaults) steering = _STEERING_;
	if (!dc["speed"].IsNull()) speed = dc["speed"].GetBool();
	else if (setDefaults) speed = _SPEED_;
	if (!dc["yawRate"].IsNull()) yawRate = dc["yawRate"].GetBool();
	else if (setDefaults) yawRate = _YAW_RATE_;
	if (!dc["location"].IsNull()) location = dc["location"].GetBool();
	else if (setDefaults) location = _LOCATION_;
	if (!dc["time"].IsNull()) time = dc["time"].GetBool();
	else if (setDefaults) time = _TIME_;


	if (!dc["exportBBox2D"].IsNull()) exportBBox2D = dc["exportBBox2D"].GetBool();
	else if (setDefaults) exportBBox2D = _EXPORT_BBOX_2D_;
	if (!dc["exportBBox2DUnprocessed"].IsNull()) exportBBox2DUnprocessed = dc["exportBBox2DUnprocessed"].GetBool();
	else if (setDefaults) exportBBox2DUnprocessed = _EXPORT_BBOX_2D_UNPROCESSED_;
	if (!dc["occlusionImage"].IsNull()) occlusionImage = dc["occlusionImage"].GetBool();
	else if (setDefaults) occlusionImage = _OCCLUSION_IMAGE_;
	if (!dc["unusedStencilIPixelmage"].IsNull()) unusedStencilIPixelmage = dc["unusedStencilIPixelmage"].GetBool();
	else if (setDefaults) unusedStencilIPixelmage = _UNUSED_STENCIL_IMAGE_;
	if (!dc["segmentationImage"].IsNull()) segmentationImage = dc["segmentationImage"].GetBool();
	else if (setDefaults) segmentationImage = _SEGMENTATION_IMAGE_;
	if (!dc["instanceSegmentationImage"].IsNull()) instanceSegmentationImage = dc["instanceSegmentationImage"].GetBool();
	else if (setDefaults) instanceSegmentationImage = _INSTANCE_SEGMENTATION_IMAGE_;
	if (!dc["instanceSegmentationImageColor"].IsNull()) instanceSegmentationImageColor = dc["instanceSegmentationImageColor"].GetBool();
	else if (setDefaults) instanceSegmentationImageColor = _INSTANCE_SEGMENTATION_IMAGE_COLOR_;
	if (!dc["exportLiDAR"].IsNull()) exportLiDAR = dc["exportLiDAR"].GetBool();
	else if (setDefaults) exportLiDAR = _EXPORT_LIDAR_;
	if (!dc["exportLiDARRaycast"].IsNull()) exportLiDARRaycast = dc["exportLiDARRaycast"].GetBool();
	else if (setDefaults) exportLiDARRaycast = _EXPORT_LIDAR_RAYCAST_;
	if (!dc["maxLidarDist"].IsNull()) maxLidarDist = dc["maxLidarDist"].GetFloat();
	else if (setDefaults) maxLidarDist = _MAX_LIDAR_DIST_;
	if (!dc["export2DPointmap"].IsNull()) export2DPointmap = dc["export2DPointmap"].GetBool();
	else if (setDefaults) export2DPointmap = _EXPORT_2D_POINTMAP_;
	if (!dc["exportSome2DPointmapText"].IsNull()) exportSome2DPointmapText = dc["exportSome2DPointmapText"].GetBool();
	else if (setDefaults) exportSome2DPointmapText = _EXPORT_SOME_2D_POINTMAP_TEXT_;
	if (!dc["exportLiDARDepthStats"].IsNull()) exportLiDARDepthStats = dc["exportLiDARDepthStats"].GetBool();
	else if (setDefaults) exportLiDARDepthStats = _EXPORT_LIDAR_DEPTH_STATS_;
	if (!dc["exportStencliBuffer"].IsNull()) exportStencliBuffer = dc["exportStencliBuffer"].GetBool();
	else if (setDefaults) exportStencliBuffer = _EXPORT_STENCIL_BUFFER_;
	if (!dc["exportStencilImage"].IsNull()) exportStencilImage = dc["exportStencilImage"].GetBool();
	else if (setDefaults) exportStencilImage = _EXPORT_STENCIL_IMAGE_;
	if (!dc["exportIndividualStencilImages"].IsNull()) exportIndividualStencilImages = dc["exportIndividualStencilImages"].GetBool();
	else if (setDefaults) exportIndividualStencilImages = _EXPORT_INDIVIDUAL_STENCIL_IMAGE_;
	if (!dc["exportDepthBuffer"].IsNull()) exportDepthBuffer = dc["exportDepthBuffer"].GetBool();
	else if (setDefaults) exportDepthBuffer = _EXPORT_DEPTH_BUFFER_;


	// [longtail] This ran on EVERY Config -- i.e. once per clip -- and the previous
	// instance was never deleted. That leaks two full frame buffers plus a D3D
	// staging texture per clip, and needlessly swaps the pointer the render thread
	// reads from inside the Present callback. Build it once and keep it unless the
	// capture resolution actually changes.
	if (screenCapturer == NULL ||
	    screenCapturer->width() != s_camParams.screenWidth ||
	    screenCapturer->height() != s_camParams.screenHeight) {
		ScreenCapturer *old = screenCapturer;
		screenCapturer = new ScreenCapturer(s_camParams.screenWidth, s_camParams.screenHeight);
		if (old != NULL) delete old;
		log("[longtail] ScreenCapturer (re)created", true);
	} else {
		log("[longtail] ScreenCapturer reused", true);
	}

	buildJSONObject();

}




void DataExport::buildJSONObject() {
	logFrame("DataExport::buildJSONObject");
	// ⚠ This runs EVERY FRAME. SetObject() drops the members but does NOT release
	// the allocator's pool -- rapidjson's MemoryPoolAllocator only grows, and frees
	// nothing until it is destroyed. Over a long capture that is unbounded growth
	// for the lifetime of the game.
	//
	// Order matters: SetObject() first (it only flips the type and zeroes the
	// member list -- no allocation), so nothing references the pool when Clear()
	// reclaims it. Clearing first would leave `d` briefly pointing at freed chunks.
	d.SetObject();
	d.GetAllocator().Clear();
	Document::AllocatorType& allocator = d.GetAllocator();
	{
		static int frames = 0;
		if ((++frames % 500) == 0) {
			std::ostringstream s;
			s << "[longtail] json frames=" << frames
			  << " pool_capacity=" << d.GetAllocator().Capacity()
			  << " bytes";
			log(s.str(), true);
		}
	}
	Value a(kArrayType);


	//TODO rename those settings properly (export_...)

	if (direction) d.AddMember("direction", a, allocator);
	if (reward) d.AddMember("reward", 0.0, allocator);
	if (throttle) d.AddMember("throttle", 0.0, allocator);
	if (brake) d.AddMember("brake", 0.0, allocator);
	if (steering) d.AddMember("steering", 0.0, allocator);
	if (speed) d.AddMember("speed", 0.0, allocator);
	if (yawRate) d.AddMember("yawRate", 0.0, allocator);
	if (location) d.AddMember("location", a, allocator);
	if (time) d.AddMember("time", a, allocator);

	// TODO add setting for those (test for those should also be made below)
	// TODO remove unnecessary ones
	d.AddMember("index", 0, allocator);
	d.AddMember("focalLen", 0.0, allocator);
	// [longtail] focalLen above is declared upstream but never assigned (always 0.0).
	// These are the fields the long-tail pipeline actually reads.
	d.AddMember("CameraFOV", 0.0, allocator);          // vertical FOV, degrees
	d.AddMember("CameraNearClip", 0.0, allocator);
	d.AddMember("CameraFarClip", 0.0, allocator);
	d.AddMember("CameraAspectRatio", 0.0, allocator);  // GRAPHICS::_GET_SCREEN_ASPECT_RATIO
	d.AddMember("GameTime", 0, allocator);             // GET_GAME_TIMER(), ms
	// [longtail] ego state -> outcome labelling client-side
	d.AddMember("EgoSpeed", 0.0, allocator);
	d.AddMember("EgoCollided", false, allocator);
	d.AddMember("EgoOnFire", false, allocator);
	d.AddMember("EgoHealth", 0, allocator);
	d.AddMember("EgoEngineHealth", 0.0, allocator);
	d.AddMember("EgoBodyHealth", 0.0, allocator);
	d.AddMember("EgoTankHealth", 0.0, allocator);
	d.AddMember("EgoRoll", 0.0, allocator);
	d.AddMember("EgoPitch", 0.0, allocator);
	d.AddMember("EgoOnRoof", false, allocator);
	d.AddMember("EgoPedHealth", 0, allocator);
	d.AddMember("EgoPedInVehicle", true, allocator);
	d.AddMember("ScreenFaded", false, allocator);
	d.AddMember("WorldReady", false, allocator);        // collision streamed in
	d.AddMember("SceneStreamed", false, allocator);     // async load scene finished
	// [longtail] road context -> coverage strata + metadata
	d.AddMember("RoadNodeDensity", 0, allocator);
	d.AddMember("RoadNodeFlags", 0, allocator);
	d.AddMember("RoadNodeValid", false, allocator);
	d.AddMember("RoadNodeDist", 0.0, allocator);     // m to nearest vehicle node
	d.AddMember("ScenarioGen", 0, allocator);       // advances on every scenario build
	d.AddMember("EgoAtLight", false, allocator);     // lawfully stopped at a red
	d.AddMember("ClipRecording", false, allocator);  // Rockstar Editor recorder is running
	// [rockstar] Editor / replay state, for driving playback from the client.
	d.AddMember("PauseMenuActive", false, allocator);
	d.AddMember("ReplayScriptRefs", 0, allocator);   // instances of replay_controller.ysc
	d.AddMember("PlayerPedExists", false, allocator);
	d.AddMember("PlayerInVehicle", false, allocator);
	d.AddMember("RenderMode", false, allocator);
	d.AddMember("RenderTarget", 0, allocator);
	d.AddMember("ScreenFadedOut", false, allocator);
	{
		Value gp(kArrayType); gp.PushBack(0.0, allocator).PushBack(0.0, allocator).PushBack(0.0, allocator);
		d.AddMember("GameplayCamPos", gp, allocator);
		Value gr(kArrayType); gr.PushBack(0.0, allocator).PushBack(0.0, allocator).PushBack(0.0, allocator);
		d.AddMember("GameplayCamRot", gr, allocator);
	}
	d.AddMember("ReplayInit", false, allocator);     // replay system initialised
	d.AddMember("ReplayAvail", false, allocator);    // replay system available (not blocked)
	d.AddMember("ReplaySpace", false, allocator);    // record space available
	// [longtail] ★ What the OTHER vehicles are doing. Every outcome measurement was
	// about the ego -- body health, decel, roll -- so "do collisions actually
	// displace and damage other traffic" was unanswerable from the data, and the
	// only way to judge it was to watch clips. These make it a number.
	d.AddMember("NearbyVehicles", 0, allocator);     // within NEARBY_R of the ego
	d.AddMember("NearbyDamaged", 0, allocator);      // ...with body health below full
	d.AddMember("NearbyOnFire", 0, allocator);       // ...burning
	d.AddMember("NearbyWrecked", 0, allocator);      // ...no longer driveable
	d.AddMember("NearbyMaxSpeed", 0.0, allocator);   // fastest of them, m/s
	d.AddMember("NearbyMinBodyHealth", 1000.0, allocator); // worst-damaged of them
	// [longtail] Position of the nearest non-ego vehicle, per frame. Exported to
	// answer one question with data instead of impressions: are NPC vehicles
	// actually jittering, or does the capture only LOOK jittery live because the
	// game is paused and unpaused several times a second? A trajectory sampled
	// from the sim is smooth or it is not; the ego's own track is the control.
	d.AddMember("NearestVehId", 0, allocator);
	d.AddMember("NearestVehPos", a, allocator);
	// [longtail] ★ Capture geometry, per frame. GTA V is DPI-unaware: on a scaled
	// display it renders into a virtualised surface and the backbuffer is SMALLER
	// than the resolution settings.xml claims. The plugin then resamples up to the
	// requested frame size, so the stream is the right shape and nothing reports
	// the discrepancy -- while meta.json's K describes a sampling grid that does
	// not exist. Ship the real backbuffer size so "were these frames actually
	// 1920x1080?" is a metadata query and not image forensics.
	d.AddMember("BackbufferWidth", 0, allocator);
	d.AddMember("BackbufferHeight", 0, allocator);
	d.AddMember("curPosition", a, allocator);
	d.AddMember("seriesIndex", a, allocator);
	d.AddMember("HeightAboveGround", 0.0, allocator);
	d.AddMember("CameraAngle", a, allocator);
	d.AddMember("CameraPosition", a, allocator);


	// Add empty fields. This is used to have the fields None in the JSON to prevent client errors
	if (exportBBox2D) d.AddMember("bbox2d", a, allocator);
	if (exportBBox2DUnprocessed) d.AddMember("bbox2dUnprocessed", a, allocator);
	if (occlusionImage) d.AddMember("occlusionImage", a, allocator);
	if (unusedStencilIPixelmage) d.AddMember("unusedStencilIPixelmage", a, allocator);
	if (segmentationImage) d.AddMember("segmentationImage", a, allocator);
	if (instanceSegmentationImage) d.AddMember("instanceSegmentationImage", a, allocator);
	if (instanceSegmentationImageColor) d.AddMember("instanceSegmentationImageColor", a, allocator);
	if (exportLiDAR) d.AddMember("LiDAR", a, allocator);
	if (exportLiDARRaycast) d.AddMember("LiDARRaycast", a, allocator);
	if (export2DPointmap) d.AddMember("2DPointmap", a, allocator);
	if (exportSome2DPointmapText) d.AddMember("Some2DPointmapText", a, allocator);
	if (exportLiDARDepthStats) d.AddMember("LiDARDepthStats", a, allocator);
	if (exportStencliBuffer) d.AddMember("StencilBuffer", a, allocator);
	if (exportStencilImage) d.AddMember("StencilImage", a, allocator);
	if (exportIndividualStencilImages) d.AddMember("IndividualStencilImage", a, allocator);
	if (exportDepthBuffer) d.AddMember("DepthBuffer", a, allocator);

	

}


// [longtail] Upstream attached the camera at a fixed (0, 0.5, 0.8) from the
// VEHICLE ORIGIN -- which is the middle of the car, so the view is from inside
// the cabin with the wheel, hands and dashboard filling much of the frame. (The
// ATTACH_CAM_TO_ENTITY calls are commented out; the position is recomputed every
// frame from cameraPositionOffset, which defaulted to {0,0,0}.)
//
// A single fixed offset cannot work across a 30-model ego pool spanning compacts
// to semi tractors. Derive it from the model's own bounding box instead: sit just
// behind the front edge, near roof height, looking forward -- a bonnet/dashcam
// mount that clears the bodywork on anything.
Vector3 DataExport::autoMountFor(Vehicle v) {
	Hash model = ENTITY::GET_ENTITY_MODEL(v);
	if (model == m_autoMountModel) return m_autoMount;
	m_autoMountModel = model;

	Vector3 mn, mx;
	GAMEPLAY::GET_MODEL_DIMENSIONS(model, &mn, &mx);

	// ★★ Mount from the DRIVER'S SEAT bone, not from a fraction of the bounding
	// box. A single fraction cannot work across a fleet whose shapes differ this
	// much -- measured bodywork intrusion at the image centre with a 0.35 forward
	// fraction: blista 13%, sandking 34%, granger 37%, dominator 42%, and a mule
	// (box truck) at 100% because 0.35 of a 3.5 m half-length puts the camera
	// INSIDE the cargo box. The bounding box says nothing about where the cabin is.
	//
	// The seat bone does, per model, for free. Offsetting forward and up from it
	// lands on the windscreen for a hatchback, an SUV and a truck cab alike.
	bool fromSeat = false;
	int bone = ENTITY::GET_ENTITY_BONE_INDEX_BY_NAME(v, const_cast<char*>("seat_dside_f"));
	if (bone >= 0) {
		Vector3 world = ENTITY::GET_WORLD_POSITION_OF_ENTITY_BONE(v, bone);
		Vector3 local = ENTITY::GET_OFFSET_FROM_ENTITY_GIVEN_WORLD_COORDS(
			v, world.x, world.y, world.z);
		// Sanity: a bone that reports outside the model's own bounds is not one we
		// should trust. Falling back is better than mounting the camera in the air.
		if (local.y > mn.y - 0.5f && local.y < mx.y + 0.5f &&
		    local.z > mn.z - 0.5f && local.z < mx.z + 1.0f) {
			m_autoMount.x = 0.0f;                       // centre line, not the seat's
			m_autoMount.y = local.y + m_seatForward;    // forward of the seat
			m_autoMount.z = local.z + m_seatUp;         // eye height above the seat
			fromSeat = true;
		}
	}
	if (!fromSeat) {
		m_autoMount.x = 0.0f;
		m_autoMount.y = mx.y * m_mountForwardFrac;
		m_autoMount.z = mx.z * m_mountUpFrac;
	}

	// ⚠ Never let the mount sit past the nose: outside the bodywork the camera
	// sees no bonnet at all, which was the original complaint in reverse.
	if (m_autoMount.y > mx.y - 0.25f) m_autoMount.y = mx.y - 0.25f;

	{
		std::ostringstream om;
		om << "[cam] mount fwd=" << m_autoMount.y << " up=" << m_autoMount.z
		   << (fromSeat ? " (from seat bone" : " (from bbox fractions")
		   << ", half-extent y=" << mx.y << " z=" << mx.z << ")";
		log(om.str(), true);
	}
	return m_autoMount;
}

void DataExport::setRenderingCam(Vehicle v) {
	logFrame("DataExport::setRenderingCam");
	Vector3 position;
	Vector3 fVec, rVec, uVec;
	Vector3 rotation = ENTITY::GET_ENTITY_ROTATION(v, 0);
	ENTITY::GET_ENTITY_MATRIX(v, &fVec, &rVec, &uVec, &position);

	Vector3 mount = autoMountFor(v);
	Vector3 total;
	total.x = mount.x + cameraPositionOffset.x;
	total.y = mount.y + cameraPositionOffset.y;
	total.z = mount.z + cameraPositionOffset.z;
	Vector3 offsetWorld = camToWorld(total, fVec, rVec, uVec);
	//Since it's offset need to subtract the cam position
	offsetWorld.x -= s_camParams.pos.x;
	offsetWorld.y -= s_camParams.pos.y;
	offsetWorld.z -= s_camParams.pos.z;

	if (m_captureFrames) {
		GAMEPLAY::SET_TIME_SCALE(0.0f);
		if (m_pauseForCapture) GAMEPLAY::SET_GAME_PAUSED(false);
		GAMEPLAY::SET_TIME_SCALE(0.0f);
	}

	//TODO fix pointer billiard, after making DataExport the owner of the camera
	CAM::SET_CAM_COORD(camera, position.x + offsetWorld.x, position.y + offsetWorld.y, position.z + offsetWorld.z);
	CAM::SET_CAM_ROT(camera, rotation.x + cameraRotationOffset.x, rotation.y + cameraRotationOffset.y, rotation.z + cameraRotationOffset.z, 0);
	
	// TODO this was added for simplicity, its ownership should be restrucutred.
	s_camParams.cameraRotationOffset = cameraRotationOffset;

	// [rockstar] The one-frame wait lets the camera move at frozen time so the
	// grab matches the pose. With frames off there is no grab; the pose is read
	// from the camera we just set, and the game keeps running.
	if (m_captureFrames) {
		scriptWait(0);
		if (m_pauseForCapture) GAMEPLAY::SET_GAME_PAUSED(true);
	}

	//std::ostringstream oss;
	//oss << "EntityID/rotation/position: " << v << "\n" <<
	//	position.x << ", " << position.y << ", " << position.z <<
	//	"\n" << rotation.x << ", " << rotation.y << ", " << rotation.z <<
	//	"\nOffset: " << offset.x << ", " << offset.y << ", " << offset.z <<
	//	"\nOffsetworld: " << offsetWorld.x << ", " << offsetWorld.y << ", " << offsetWorld.z;
	//log(oss.str());
}



void DataExport::setCameraPositionAndRotation(float x, float y, float z, float rot_x, float rot_y, float rot_z) {
	cameraPositionOffset.x = x;
	cameraPositionOffset.y = y;
	cameraPositionOffset.z = z;
	cameraRotationOffset.x = rot_x;
	cameraRotationOffset.y = rot_y;
	cameraRotationOffset.z = rot_z;
}



// [rockstar] The time-freeze half of setRenderingCam() with no camera update:
// the same SET_TIME_SCALE(0) / one-frame wait so the grab and the pose describe
// one frozen instant, for a capture that renders whatever camera is active.
void DataExport::freezeFrameNoCam() {
	if (!m_captureFrames) return;
	GAMEPLAY::SET_TIME_SCALE(0.0f);
	if (m_pauseForCapture) GAMEPLAY::SET_GAME_PAUSED(false);
	GAMEPLAY::SET_TIME_SCALE(0.0f);
	scriptWait(0);
	if (m_pauseForCapture) GAMEPLAY::SET_GAME_PAUSED(true);
}

StringBuffer DataExport::generateMessage() {
	logFrame("DataExport::GenerateMessage");

	buildJSONObject();

	//StringBuffer buffer(0, 131072);
	StringBuffer buffer;
	buffer.Clear();
	Writer<StringBuffer> writer(buffer);


	if (m_captureFrames) {
		if (m_pauseForCapture) GAMEPLAY::SET_GAME_PAUSED(true);
		GAMEPLAY::SET_TIME_SCALE(0.0f);
	}

	{
		// [rockstar] No ego (render mode before a target is locked, or a replay
		// camera the user wants as-is): freeze the frame without moving our cam.
		Vehicle ev = egoHandle();
		if (ev) setRenderingCam(ev);
		else freezeFrameNoCam();
	}

	////Can check whether camera and vehicle are aligned
	//Vector3 camRot2 = CAM::GET_CAM_ROT(camera, 0);
	//std::ostringstream oss1;
	//oss1 << "entityRotation X: " << rotation.x << " Y: " << rotation.y << " Z: " << rotation.z <<
	//    "\n camRot X: " << camRot.x << " Y: " << camRot.y << " Z: " << camRot.z <<
	//    "\n camRot2 X: " << camRot2.x << " Y: " << camRot2.y << " Z: " << camRot2.z;
	//std::string str1 = oss1.str();
	//log(str1);


	// Setting bufffers for the Server
	// Those were the original commands from DeepGTAV
	// They each need Scenario::setVehicleList(), Scenario::setPedsList() etc.
	// Those commands would set the JSON document, e.g. d["vehicles"] = _vehicles;

	// Those functionalities have been somehow moved to ObjectDetection.cpp, e.g. ObjectDetection::setVehiclesList()
	// To fix the JSON / Server those have to be implemented again to correctly set e.g. d["vehicles"] = _vehicles
	// A quick fix would be to copy the old Definitions from DeepGTAV, but I don't know if there would be side effects
	// The correct solution would be to integrate the functionalities of e.g. Scenario::setVehiclesList() and ObjectDetection::setVehiclesList()
	// into one.
	//
	// For now i only implement the messages I need and have the rest commented out.

	// if (vehicles) setVehiclesList();
	// if (peds) setPedsList();
	// if (trafficSigns); //TODO
	//if (direction) setDirection(); // TODO add again
	if (reward) exportReward();
	if (throttle) exportThrottle();
	if (brake) exportBrake();
	if (steering) exportSteering();
	if (speed) exportSpeed();
	if (yawRate) exportYawRate();
	if (location) exportLocation();
	if (time) exportTime();
	exportHeightAboveGround();
	exportCameraPosition();
	exportCameraAngle();
	exportCameraIntrinsics();   // [longtail]
	exportTrafficReaction();    // [longtail]
	exportGameTime();           // [longtail]
	exportEgoState();           // [longtail]
	exportRoadContext();        // [longtail]



	// TODO legacy functions from ObjectDetection: 
	//exportPosition();
	//exportCalib();
	//setGroundPlanePoints();


#ifndef LONGTAIL_SLIM
	if (!m_pObjDet) {
		m_pObjDet.reset(new ObjectDetection());
		m_pObjDet->initCollection(s_camParams.width, s_camParams.height, false, instance_index, maxLidarDist);
		
	}
#endif // LONGTAIL_SLIM


	if (recording_active) {
		if (m_captureFrames) capture();

		setCamParams();
		//setColorBuffer();
#ifndef LONGTAIL_SLIM
		BufferSizes bufferSizes = m_pObjDet->setDepthAndStencil();

		// check if the buffer sizes are actually correct, if not skip this export
		// They could be wrong due to errors in NVIDIA DSR
		if (bufferSizes.DepthBufferSize == s_camParams.width * s_camParams.height * 4
			&& bufferSizes.StencilBufferSize == s_camParams.width * s_camParams.height) {

			// TODO this was for secondary perspective capture, it could be removed
			m_pObjDet->passEntity();






			////Create vehicles if it is a stationary scenario
			//createVehicles();

			//if (GENERATE_SECONDARY_PERSPECTIVES) {
			//	generateSecondaryPerspectives();
			//}

			//For testing to ensure secondary ownvehicle aligns with main perspective
			//generateSecondaryPerspective(m_pObjDet->m_ownVehicleObj);

			FrameObjectInfo fObjInfo = m_pObjDet->generateMessage();

			// TODO remove?
			d["index"] = fObjInfo.instanceIdx;

			Document::AllocatorType& allocator = d.GetAllocator();

			if (exportBBox2D) d.AddMember("bbox2d", m_pObjDet->exportDetectionsString(fObjInfo), allocator);
			if (exportBBox2DUnprocessed) d.AddMember("bbox2dUnprocessed", m_pObjDet->exportDetectionsStringUnprocessed(fObjInfo), allocator);
			if (occlusionImage) d.AddMember("occlusionImage", m_pObjDet->outputOcclusion(), allocator);
			if (unusedStencilIPixelmage) d.AddMember("unusedStencilIPixelmage", m_pObjDet->outputUnusedStencilPixels(), allocator);

			// TODO this is not clean right now, make this better later
			// Export different Segmentation images:
			if (segmentationImage) d.AddMember("segmentationImage", m_pObjDet->exportSegmentationImage(), allocator);
			if (instanceSegmentationImage) d.AddMember("instanceSegmentationImage", m_pObjDet->printInstanceSegmentationImage(), allocator);
			if (instanceSegmentationImageColor) d.AddMember("instanceSegmentationImageColor", m_pObjDet->printInstanceSegmentationImageColor(), allocator);
			if (exportLiDAR) d.AddMember("LiDAR", m_pObjDet->exportLiDAR(), allocator);
			if (exportLiDARRaycast) d.AddMember("LiDARRaycast", m_pObjDet->exportLiDARRaycast(), allocator);
			if (export2DPointmap) d.AddMember("2DPointmap", m_pObjDet->export2DPointmap(), allocator);
			if (exportSome2DPointmapText) d.AddMember("Some2DPointmapText", m_pObjDet->exportSome2DPointmapText(), allocator);
			if (exportLiDARDepthStats) d.AddMember("LiDARDepthStats", m_pObjDet->exportLidarDepthStats(), allocator);
			if (exportStencliBuffer) d.AddMember("StencilBuffer", m_pObjDet->exportStencilBuffer(), allocator);
			if (exportStencilImage) d.AddMember("StencilImage", m_pObjDet->exportStencilImage(), allocator);
			if (exportIndividualStencilImages) d.AddMember("IndividualStencilImage", m_pObjDet->exportIndividualStencilImages(), allocator);
			if (exportDepthBuffer) d.AddMember("DepthBuffer", m_pObjDet->exportDepthBuffer(), allocator);
		}

		m_pObjDet->refreshBuffers();
		m_pObjDet->increaseIndex();
#endif // LONGTAIL_SLIM
	}

	d.Accept(writer);

	//log("Message JSON");
	//log(buffer.GetString());
	//log("End Message");

	if (m_captureFrames) {
		if (m_pauseForCapture) GAMEPLAY::SET_GAME_PAUSED(false);
		// [longtail] was a hard-coded 1.0f; now a knob (see Scenario::setTimeScale)
		GAMEPLAY::SET_TIME_SCALE(m_resumeTimeScale);
	}

	return buffer;
}



void DataExport::setCamParams() {
	logFrame("DataExport::setCamParams");
	//These values stay the same throughout a collection period
	if (!s_camParams.init) {
		s_camParams.nearClip = CAM::GET_CAM_NEAR_CLIP(camera);
		s_camParams.farClip = CAM::GET_CAM_FAR_CLIP(camera);
		s_camParams.fov = CAM::GET_CAM_FOV(camera);
		s_camParams.ncHeight = 2 * s_camParams.nearClip * tan(s_camParams.fov / 2. * (PI / 180.)); // field of view is returned vertically
		s_camParams.ncWidth = s_camParams.ncHeight * GRAPHICS::_GET_SCREEN_ASPECT_RATIO(false);
		s_camParams.init = true;

		//if (m_recordScenario) {
		//	float gameFC = CAM::GET_CAM_FAR_CLIP(camera);
		//	std::ostringstream oss;
		//	oss << "NC, FC (gameFC), FOV: " << s_camParams.nearClip << ", " << s_camParams.farClip << " (" << gameFC << "), " << s_camParams.fov;
		//	std::string str = oss.str();
		//	log(str, true);
		//}
	}

	//These values change frame to frame
	s_camParams.theta = CAM::GET_CAM_ROT(camera, 0);
	s_camParams.pos = CAM::GET_CAM_COORD(camera);

	//std::ostringstream oss1;
	//oss1 << "\ns_camParams.pos X: " << s_camParams.pos.x << " Y: " << s_camParams.pos.y << " Z: " << s_camParams.pos.z <<
	//	"\nvehicle.pos X: " << currentPos.x << " Y: " << currentPos.y << " Z: " << currentPos.z <<
	//	"\nfar: " << s_camParams.farClip << " nearClip: " << s_camParams.nearClip << " fov: " << s_camParams.fov <<
	//	"\nrotation gameplay: " << s_camParams.theta.x << " Y: " << s_camParams.theta.y << " Z: " << s_camParams.theta.z <<
	//	"\n AspectRatio: " << GRAPHICS::_GET_SCREEN_ASPECT_RATIO(false);
	//std::string str1 = oss1.str();
	//log(str1);

	//For optimizing 3d to 2d and unit vector to 2d calculations
	s_camParams.eigenPos = Eigen::Vector3f(s_camParams.pos.x, s_camParams.pos.y, s_camParams.pos.z);
	s_camParams.eigenRot = Eigen::Vector3f(s_camParams.theta.x, s_camParams.theta.y, s_camParams.theta.z);
	s_camParams.eigenTheta = (PI / 180.0) * s_camParams.eigenRot;
	s_camParams.eigenCamDir = rotate(WORLD_NORTH, s_camParams.eigenTheta);
	s_camParams.eigenCamUp = rotate(WORLD_UP, s_camParams.eigenTheta);
	s_camParams.eigenCamEast = rotate(WORLD_EAST, s_camParams.eigenTheta);
	s_camParams.eigenClipPlaneCenter = s_camParams.eigenPos + s_camParams.nearClip * s_camParams.eigenCamDir;
	s_camParams.eigenCameraCenter = -s_camParams.nearClip * s_camParams.eigenCamDir;

	//For measuring height of camera (LiDAR) to ground plane
	/*float groundZ;
	GAMEPLAY::GET_GROUND_Z_FOR_3D_COORD(s_camParams.pos.x, s_camParams.pos.y, s_camParams.pos.z, &(groundZ), 0);

	std::ostringstream oss;
	oss << "LiDAR height: " << s_camParams.pos.z - groundZ;
	std::string str = oss.str();
	log(str);*/
	logFrame("DataExport::setCamParams END");
}



void DataExport::capture() {
	logFrame("DataExport::capture");
	//Time synchronization seems to be correct with 2 render calls
	CAM::RENDER_SCRIPT_CAMS(TRUE, FALSE, 0, FALSE, FALSE);
	scriptWait(0);
	CAM::RENDER_SCRIPT_CAMS(TRUE, FALSE, 0, FALSE, FALSE);
	scriptWait(0);
	CAM::RENDER_SCRIPT_CAMS(TRUE, FALSE, 0, FALSE, FALSE);
	scriptWait(0);
	screenCapturer->capture();
}


void DataExport::setRecording_active(bool x) {
	log(std::string("[longtail] setRecording_active ") + (x ? "TRUE" : "FALSE") +
	    (screenCapturer ? " (capturer present)" : " (NO CAPTURER YET)"), true);
	recording_active = x;
	if (screenCapturer) screenCapturer->setEnabled(x && m_captureFrames);
	// [longtail] The capture reads the swapchain backbuffer, so everything the
	// game draws is baked into the frames. Persistent off-switches here; the
	// per-frame call in Scenario::hideHudThisFrame() is what actually holds.
	UI::DISPLAY_HUD(x ? FALSE : TRUE);
	UI::DISPLAY_RADAR(x ? FALSE : TRUE);
	UI::DISPLAY_CASH(x ? FALSE : TRUE);
}


////Generate a secondary perspective for all nearby vehicles
//void DataExport::generateSecondaryPerspectives() {
//	for (ObjEntity v : m_pObjDet->m_nearbyVehicles) {
//		if (VEHICLE::IS_THIS_MODEL_A_CAR(v.model)) {
//			generateSecondaryPerspective(v);
//		}
//	}
//	m_pObjDet->m_nearbyVehicles.clear();
//}
//
//void DataExport::generateSecondaryPerspective(ObjEntity vInfo) {
//	setRenderingCam(vInfo.entityID, vInfo.height, vInfo.length);
//
//	//if (m_pauseForCapture) GAMEPLAY::SET_GAME_PAUSED(true);
//	capture();
//
//	setCamParams();
//	setDepthBuffer();
//	setStencilBuffer();
//
//	FrameObjectInfo fObjInfo = m_pObjDet->generateMessage(depth_map, m_stencilBuffer, vInfo.entityID);
//	m_pObjDet->exportDetections(fObjInfo, &vInfo);
//	std::string filename = m_pObjDet->getStandardFilename("image_2", ".png");
//	m_pObjDet->exportImage(screenCapturer->pixels, filename);
//
//	//if (m_pauseForCapture) GAMEPLAY::SET_GAME_PAUSED(false);
//}




/*
 ********************************************************************
 Set Individual JSON fields
 ********************************************************************
*/

void DataExport::exportThrottle() {
	Vehicle ev = egoHandle(); d["throttle"] = ev ? getFloatValue(ev, 0x92C) : 0.0f;
}

void DataExport::exportBrake() {
	Vehicle ev = egoHandle(); d["brake"] = ev ? getFloatValue(ev, 0x930) : 0.0f;
}

void DataExport::exportSteering() {
	Vehicle ev = egoHandle(); d["steering"] = ev ? -getFloatValue(ev, 0x924) / 0.6981317008 : 0.0;
}

void DataExport::exportSpeed() {
	Vehicle ev = egoHandle(); d["speed"] = ev ? ENTITY::GET_ENTITY_SPEED(ev) : 0.0f;
}

void DataExport::exportYawRate() {
	Vehicle ev = egoHandle(); Vector3 rates; if (ev) rates = ENTITY::GET_ENTITY_ROTATION_VELOCITY(ev); else { rates.x = rates.y = rates.z = 0.0f; }
	d["yawRate"] = rates.z*180.0 / 3.14159265359;
}

void DataExport::exportLocation() {
	Document::AllocatorType& allocator = d.GetAllocator();
	Vehicle ev = egoHandle(); Vector3 pos; if (ev) pos = ENTITY::GET_ENTITY_COORDS(ev, false); else { pos.x = pos.y = pos.z = 0.0f; }
	Value location(kArrayType);
	location.PushBack(pos.x, allocator).PushBack(pos.y, allocator).PushBack(pos.z, allocator);
	d["location"] = location;
}

void DataExport::exportTime() {
	Document::AllocatorType& allocator = d.GetAllocator();
	Value time(kArrayType);
	time.PushBack(TIME::GET_CLOCK_HOURS(), allocator).PushBack(TIME::GET_CLOCK_MINUTES(), allocator).PushBack(TIME::GET_CLOCK_SECONDS(), allocator);
	d["time"] = time;
}

void DataExport::exportHeightAboveGround() {
	Vehicle ev = egoHandle(); Vector3 pos; if (ev) pos = ENTITY::GET_ENTITY_COORDS(ev, false); else { pos.x = pos.y = pos.z = 0.0f; }
	float waterZ;
	WATER::GET_WATER_HEIGHT(pos.x, pos.y, pos.z, &waterZ);
	float heightAboveWater = pos.z - waterZ;
	float height = ev ? std::min(heightAboveWater, ENTITY::GET_ENTITY_HEIGHT_ABOVE_GROUND(ev)) : 0.0f;

	d["HeightAboveGround"] = height;
}

//void DataExport::setDirection() {
//	int direction;
//	float distance;
//	Vehicle temp_vehicle;
//	Document::AllocatorType& allocator = d.GetAllocator();
//	PATHFIND::GENERATE_DIRECTIONS_TO_COORD(dir.x, dir.y, dir.z, TRUE, &direction, &temp_vehicle, &distance);
//	Value _direction(kArrayType);
//	_direction.PushBack(direction, allocator).PushBack(distance, allocator);
//	d["direction"] = _direction;
//}

void DataExport::exportReward() {
	Vehicle ev = egoHandle(); d["reward"] = ev ? rewarder->computeReward(ev) : 0.0f;
}

void DataExport::exportCameraPosition() {
	Document::AllocatorType& allocator = d.GetAllocator();
	Vector3 pos = CAM::GET_CAM_COORD(camera);
	Value position(kArrayType);
	position.PushBack(pos.x, allocator).PushBack(pos.y, allocator).PushBack(pos.z, allocator);
	d["CameraPosition"] = position;
}

// [longtail] Everything the client needs to label what actually happened in a
// clip. Setup entropy is not outcome entropy: without this the generator records
// what it ASKED for and never what occurred.
// ⚠ IS_ENTITY_ON_FIRE lives in the FIRE namespace, not ENTITY.
void DataExport::exportEgoState() {
	Vehicle v = egoHandle();
	Ped p = PLAYER::PLAYER_PED_ID();

	bool haveV = (v != NULL) && ENTITY::DOES_ENTITY_EXIST(v);
	d["EgoSpeed"] = haveV ? ENTITY::GET_ENTITY_SPEED(v) : 0.0f;
	d["EgoCollided"] = haveV ? (ENTITY::HAS_ENTITY_COLLIDED_WITH_ANYTHING(v) != 0) : false;
	d["EgoOnFire"] = haveV ? (FIRE::IS_ENTITY_ON_FIRE(v) != 0) : false;
	// ⚠ A lawful ego STOPS at red lights, and the client's stuck detector read
	// that as a bad spawn -- a both_sane variation was rejected for "immobile 4 s
	// before any impact" while waiting at a junction. Export the reason it stopped.
	d["EgoAtLight"] = haveV ? (VEHICLE::IS_VEHICLE_STOPPED_AT_TRAFFIC_LIGHTS(v) != 0) : false;
	d["ClipRecording"] = UNK1::_IS_RECORDING() != 0;
	{
		Ped pp = PLAYER::PLAYER_PED_ID();
		const bool ppOk = ENTITY::DOES_ENTITY_EXIST(pp) != 0;
		d["PauseMenuActive"] = UI::IS_PAUSE_MENU_ACTIVE() != 0;
		// GET_NUMBER_OF_REFERENCES_OF_SCRIPT_WITH_NAME_HASH, absent from this header; by hash.
		d["ReplayScriptRefs"] = invoke<int>(0x2C83A9DA6BFFC4F9, GAMEPLAY::GET_HASH_KEY("replay_controller"));
		d["PlayerPedExists"] = ppOk;
		d["PlayerInVehicle"] = ppOk && PED::IS_PED_IN_ANY_VEHICLE(pp, FALSE);
		d["RenderMode"] = m_renderMode;
		d["RenderTarget"] = (int)m_renderTarget;
		d["ScreenFadedOut"] = CAM::IS_SCREEN_FADED_OUT() != 0;
		Vector3 gp = CAM::GET_GAMEPLAY_CAM_COORD();
		Vector3 gr = CAM::GET_GAMEPLAY_CAM_ROT(0);
		d["GameplayCamPos"][0] = gp.x; d["GameplayCamPos"][1] = gp.y; d["GameplayCamPos"][2] = gp.z;
		d["GameplayCamRot"][0] = gr.x; d["GameplayCamRot"][1] = gr.y; d["GameplayCamRot"][2] = gr.z;
	}
	d["ReplayInit"]  = UNK1::_0xDF4B952F7D381B95() != 0;
	d["ReplayAvail"] = UNK1::_0x4282E08174868BE3() != 0;
	d["ReplaySpace"] = UNK1::_0x33D47E85B476ABCD(TRUE) != 0;
	d["ScenarioGen"] = (int)m_scenarioGen;
	d["EgoHealth"] = haveV ? ENTITY::GET_ENTITY_HEALTH(v) : 0;
	d["EgoEngineHealth"] = haveV ? VEHICLE::GET_VEHICLE_ENGINE_HEALTH(v) : 0.0f;
	d["EgoBodyHealth"] = haveV ? VEHICLE::GET_VEHICLE_BODY_HEALTH(v) : 0.0f;
	d["EgoTankHealth"] = haveV ? VEHICLE::GET_VEHICLE_PETROL_TANK_HEALTH(v) : 0.0f;
	d["EgoRoll"] = haveV ? ENTITY::GET_ENTITY_ROLL(v) : 0.0f;
	d["EgoPitch"] = haveV ? ENTITY::GET_ENTITY_PITCH(v) : 0.0f;
	d["EgoOnRoof"] = haveV ? (VEHICLE::IS_VEHICLE_STUCK_ON_ROOF(v) != 0) : false;

	bool haveP = (p != NULL) && ENTITY::DOES_ENTITY_EXIST(p);
	d["EgoPedHealth"] = haveP ? ENTITY::GET_ENTITY_HEALTH(p) : 0;
	d["EgoPedInVehicle"] = (haveP && haveV) ? (PED::IS_PED_IN_VEHICLE(p, v, TRUE) != 0) : false;

	// Should always be false -- Scenario::enforceGameOverGuards cancels fades the
	// frame it sees them. Exported so the client can drop a clip if one slips through.
	d["ScreenFaded"] = (CAM::IS_SCREEN_FADED_OUT() || CAM::IS_SCREEN_FADING_OUT()) ? true : false;

	// [longtail] Warmup gating. The client waits on THESE, not on a wall-clock
	// timer -- a fixed warmup is a fixed tax on every clip, and for a 5 s clip a
	// 20 s warmup means 80% of the run is warmup.
	d["WorldReady"] = haveV ? (ENTITY::HAS_COLLISION_LOADED_AROUND_ENTITY(v) != 0) : false;
	d["SceneStreamed"] = (STREAMING::IS_NEW_LOAD_SCENE_LOADED() != 0);
}

// [longtail] What kind of road are we actually on? The coverage sampler stratifies
// by hand-drawn map boxes, but road class is the variable that decides what an
// event looks like -- a red-light runner in a 4-lane junction and one on a rural
// single-track are different data.
// [longtail] Traffic reaction, measured rather than eyeballed. Cheap: one
// worldGetAllVehicles sweep and a handful of getters, on the same frame cadence
// as everything else here.
void DataExport::exportTrafficReaction() {
	Vehicle ego = egoHandle();
	if (ego == NULL || !ENTITY::DOES_ENTITY_EXIST(ego)) return;
	Vector3 ep = ENTITY::GET_ENTITY_COORDS(ego, false);

	const float NEARBY_R = 60.0f;
	const int MAXV = 128;
	int vehs[MAXV];
	int n = worldGetAllVehicles(vehs, MAXV);
	// ⚠ NOT `near`: windows.h still defines it as a legacy pointer macro, so the
	// declaration silently becomes `int  = 0, ...` and the errors point elsewhere.
	int nNear = 0, damaged = 0, onfire = 0, wrecked = 0;
	float fastest = 0.0f;
	float worstHealth = 1000.0f;
	float nearestD2 = 1e18f;
	Vehicle nearestV = 0;
	Vector3 nearestPos; nearestPos.x = nearestPos.y = nearestPos.z = 0.0f;
	for (int i = 0; i < n; ++i) {
		Vehicle v = vehs[i];
		if (v == ego || !ENTITY::DOES_ENTITY_EXIST(v)) continue;
		Vector3 vp = ENTITY::GET_ENTITY_COORDS(v, false);
		const float dx = vp.x - ep.x, dy = vp.y - ep.y, dz = vp.z - ep.z;
		if (dx * dx + dy * dy + dz * dz > NEARBY_R * NEARBY_R) continue;
		++nNear;
		// 1000 is full. Anything below means this vehicle has been hit by
		// something -- which is the question being asked.
		if (VEHICLE::GET_VEHICLE_BODY_HEALTH(v) < 995.0f) ++damaged;
		// ⚠ IS_ENTITY_ON_FIRE lives in FIRE, not ENTITY, despite the name.
		if (FIRE::IS_ENTITY_ON_FIRE(v)) ++onfire;
		if (!VEHICLE::IS_VEHICLE_DRIVEABLE(v, FALSE)) ++wrecked;
		const float sp = ENTITY::GET_ENTITY_SPEED(v);
		if (sp > fastest) fastest = sp;
		// ⚠ Pick the ignition threshold from this rather than guessing it. The first
		// guess (400) produced zero fires across 26 clips, which says nothing about
		// whether ignition works -- only that nothing got that far down.
		const float bh = VEHICLE::GET_VEHICLE_BODY_HEALTH(v);
		if (bh < worstHealth) worstHealth = bh;
		const float d2 = dx * dx + dy * dy + dz * dz;
		if (d2 < nearestD2) { nearestD2 = d2; nearestV = v; nearestPos = vp; }
	}
	d["NearbyVehicles"] = nNear;
	d["NearbyDamaged"] = damaged;
	d["NearbyOnFire"] = onfire;
	d["NearbyWrecked"] = wrecked;
	d["NearbyMaxSpeed"] = (double)fastest;
	d["NearbyMinBodyHealth"] = (double)worstHealth;

	// Track a single, identified vehicle rather than "whatever is closest this
	// frame" -- the id has to be exported alongside, or a change of subject looks
	// exactly like a position jump.
	Document::AllocatorType& alloc = d.GetAllocator();
	Value np(kArrayType);
	np.PushBack(nearestPos.x, alloc).PushBack(nearestPos.y, alloc).PushBack(nearestPos.z, alloc);
	d["NearestVehId"] = (int)nearestV;
	d["NearestVehPos"] = np;
}

void DataExport::exportRoadContext() {
	Vehicle v = egoHandle();
	if (v == NULL || !ENTITY::DOES_ENTITY_EXIST(v)) {
		d["RoadNodeValid"] = false;
		d["RoadNodeDist"] = 0.0;
		return;
	}
	Vector3 pos = ENTITY::GET_ENTITY_COORDS(v, false);
	// the native takes Any* (== DWORD*), not int*
	Any density = 0, flags = 0;
	BOOL ok = PATHFIND::GET_VEHICLE_NODE_PROPERTIES(pos.x, pos.y, pos.z, &density, &flags);
	d["RoadNodeValid"] = (ok != 0);
	d["RoadNodeDensity"] = (int)density;
	d["RoadNodeFlags"] = (int)flags;

	// [longtail] RoadNodeValid is a yes/no from the pathfind grid and stays TRUE
	// well after the car has left the carriageway, so it cannot gate an "is the
	// ego off-road" decision on its own. Ship the distance to the nearest node as
	// well: it is continuous, and the client thresholds it (nodes sit on the
	// centreline, so ~10 m is still on a wide road).
	Vector3 node; node.x = node.y = node.z = 0.0f;
	if (PATHFIND::GET_CLOSEST_VEHICLE_NODE(pos.x, pos.y, pos.z, &node, 1, 3.0f, 0.0f)) {
		const float dx = node.x - pos.x, dy = node.y - pos.y;
		d["RoadNodeDist"] = (double)sqrtf(dx * dx + dy * dy);
	} else {
		d["RoadNodeDist"] = 999.0;      // no node found: deep off-network
	}

	// [longtail] ⚠ Two alternatives were measured and BOTH rejected; recorded so
	// they are not retried:
	//
	//   IS_POINT_ON_ROAD (0x125BF4ABFC536B09) resolves fine on 1.0.3889.0, but it
	//   is FALSE on 3% of frames sitting 0.4-3.4 m from a node -- closer to the
	//   road than the frames it calls true (median 4.1 m). Not a usable road test.
	//
	//   Scaling the threshold by local node SPACING, on the theory that rural
	//   nodes are sparser. They are not: a forced rural run measured p95 distance
	//   of 6.8 m (west_highway) and 9.0 m (mt_chiliad) against 5.8-9.0 m urban.
	//   The production clips reading 33-37 m in those regions were genuinely off
	//   the carriageway, and the fixed threshold was right to reject them. Rural
	//   roads are narrower and kerbless, so an aggressive style plus a collision
	//   puts the car in the scrub more easily -- a real phenomenon, not an
	//   artefact of the measurement.
}

// [longtail] Emit everything needed to build a pinhole K client-side.
// s_camParams.fov/nearClip/farClip are cached behind s_camParams.init and only
// recomputed when init is cleared, so we re-read FOV from the native each frame:
// a scenario that changes FOV mid-clip would otherwise silently ship stale
// intrinsics.
void DataExport::exportCameraIntrinsics() {
	float fov = CAM::GET_CAM_FOV(camera);
	float aspect = GRAPHICS::_GET_SCREEN_ASPECT_RATIO(false);
	d["CameraFOV"] = fov;
	d["CameraNearClip"] = CAM::GET_CAM_NEAR_CLIP(camera);
	d["CameraFarClip"] = CAM::GET_CAM_FAR_CLIP(camera);
	d["CameraAspectRatio"] = aspect;

	// ★ The real backbuffer, alongside the frame size we are claiming. If these
	// disagree the frames are resampled and K is a fiction -- see the note on the
	// BackbufferWidth schema entry. Zero until the first Present has been grabbed.
	if (screenCapturer != NULL) {
		d["BackbufferWidth"] = screenCapturer->backbufferWidth();
		d["BackbufferHeight"] = screenCapturer->backbufferHeight();
	}
}

// [longtail] Engine clock in ms. Frame intervals are NOT uniform; the client
// uses this to record real dt rather than assuming 1/rate.
void DataExport::exportGameTime() {
	// GET_GAME_TIMER returns Any (DWORD); rapidjson cannot disambiguate that
	d["GameTime"] = (int64_t)GAMEPLAY::GET_GAME_TIMER();
}

void DataExport::exportCameraAngle() {
	Document::AllocatorType& allocator = d.GetAllocator();
	Vector3 ang = CAM::GET_CAM_ROT(camera, 0);
	Value angles(kArrayType);
	angles.PushBack(ang.x, allocator).PushBack(ang.y, allocator).PushBack(ang.z, allocator);
	d["CameraAngle"] = angles;
}

//void DataExport::exportWeather() {
//	d["Weather"] = ...;
//}




