#include "Server.h"
//#include <thread>

#include <string>
#include <sstream>
#include <fstream>
#include <cstdlib>

#include "lib/rapidjson/stringbuffer.h"
#include "lib/main.h"
#include "Functions.h"

//#include <zmqpp.hpp>

#include <zmq.hpp>


//using namespace rapidjson;
using namespace std;

Server::Server(unsigned int port) {
	log("Server::Server initialize");
	socket = zmq::socket_t(ctx, zmq::socket_type::pair);

	// ⚠⚠ THE memory bug. checkSendMessage() does socket.send(buffer(pixels, len)),
	// which COPIES the frame into a ZMQ message. ZMQ then queues up to the send
	// high-water mark before applying backpressure -- and the default SNDHWM is
	// 1000 messages. At 320x160 (upstream's default) a frame is 150 KB, so a full
	// queue is 150 MB and nobody ever noticed. At 1920x1080 a frame is 6.2 MB, so
	// a full queue is 6.2 GB: the game grows ~50 MB/s while capturing and dies of
	// memory exhaustion -- silently, with no ScriptHookV exception -- at almost
	// exactly 1000 frames.
	//
	// Cap the queue so the plugin blocks on send instead of buffering. Blocking is
	// the correct behaviour here: it is real backpressure, the client is supposed
	// to consume every frame, and a briefly slower game beats an OOM kill.
	// HWM must be set BEFORE bind() to take effect.
	socket.set(zmq::sockopt::sndhwm, 8);      // <= ~50 MB queued at 1080p
	socket.set(zmq::sockopt::rcvhwm, 8);
	socket.set(zmq::sockopt::linger, 0);      // don't hang on shutdown
	// [longtail] Upstream hardcoded "tcp://127.0.0.1:8000" here, silently discarding
	// the `port` argument. Honour it, and allow the bind address to be overridden.
	//
	// The default stays LOOPBACK ONLY -- this is an unauthenticated control socket
	// that can spawn traffic and move the camera, so it must not be reachable off
	// the machine. A WSL2 client under NAT cannot see Windows loopback; the fix for
	// that is to set DEEPGTAV_BIND to the host's WSL-facing vEthernet address
	// (host-internal, not LAN-visible), NOT to 0.0.0.0.
	std::string bindHost;
	const char* env = getenv("DEEPGTAV_BIND");
	if (env != NULL && *env != '\0') bindHost = env;

	// A Steam-launched game inherits Steam's environment, so DEEPGTAV_BIND cannot
	// be set per-launch. Also accept a one-line file in the game directory.
	if (bindHost.empty()) {
		std::ifstream f("DeepGTAV_bind.txt");
		if (f) {
			std::getline(f, bindHost);
			while (!bindHost.empty() &&
			       (bindHost.back() == '\r' || bindHost.back() == '\n' ||
			        bindHost.back() == ' '  || bindHost.back() == '\t'))
				bindHost.erase(bindHost.size() - 1);
		}
	}

	if (bindHost.empty()) bindHost = "127.0.0.1";
	std::ostringstream ep;
	ep << "tcp://" << bindHost << ":" << port;
	log("Server::Server binding " + ep.str());
	socket.bind(ep.str());
}

//void Server::checkClient() {
//
//}

void Server::checkRecvMessage() {
	//log("Server::checkRecvMessage");

	zmq::message_t message;

	zmq::pollitem_t items[] = {
		{static_cast<void*>(socket), 0, ZMQ_POLLIN, 0}
	};

	zmq::poll(&items[0], 1, 1);

	if (items[0].revents & ZMQ_POLLIN) {
		socket.recv(&message);
		string jsonText;
		Document d;
		jsonText = message.to_string();

		d.Parse(jsonText);



		// Handle received Message:

		if (d.HasMember("commands")) {
			printf("Commands received\n");
			const Value& commands = d["commands"];
			scenario.setCommands(commands["throttle"].GetFloat(), commands["brake"].GetFloat(), commands["steering"].GetFloat());
		}
		else if (d.HasMember("config")) {
			//Change the message values and keep the others the same
			printf("Config received\n");
			const Value& config = d["config"];
			const Value& sc = config["scenario"];
			const Value& dc = config["dataset"];
			scenario.config(sc, dc);
		}
		else if (d.HasMember("start")) {
			//Set the message values and randomize the others. Start sending the messages
			printf("Start received\n");
			const Value& config = d["start"];
			const Value& sc = config["scenario"];
			const Value& dc = config["dataset"];
			scenario.start(sc, dc);

			clientStarted = true;
			//sendOutputs = true;
		}
		else if (d.HasMember("stop")) {
			//Stop sendig messages, keep client connected
			printf("Stop received\n");
			//sendOutputs = false;
			scenario.stop();
			clientStarted = false;
		}
		else if (d.HasMember("StartRecording")) {
			scenario.setRecording_active(true);
		}
		else if (d.HasMember("StopRecording")) {
			scenario.setRecording_active(false);
		}
		else if (d.HasMember("GoToLocation")) {
			const Value& target = d["GoToLocation"];
			scenario.goToLocation(target["x"].GetFloat(), target["y"].GetFloat(), target["z"].GetFloat(), target["speed"].GetFloat());
		}
		else if (d.HasMember("TeleportToLocation")) {
			const Value& target = d["TeleportToLocation"];
			scenario.teleportToLocation(target["x"].GetFloat(), target["y"].GetFloat(), target["z"].GetFloat());
		}
		else if (d.HasMember("SetCameraPositionAndRotation")) {
			printf("New Camera Settings received\n");
			const Value& camset = d["SetCameraPositionAndRotation"];
			scenario.setCameraPositionAndRotation(camset["x"].GetFloat(), camset["y"].GetFloat(), camset["z"].GetFloat(), camset["rot_x"].GetFloat(), camset["rot_y"].GetFloat(), camset["rot_z"].GetFloat());
		}
		else if (d.HasMember("CreatePed")) {
			const Value& pd = d["CreatePed"];
			// [longtail] `task` has been in the Python message since forever and was
			// simply never read here -- which is why PedestrianHazard produced a ped
			// standing on the pavement rather than a dart-out.
			int ptask = pd.HasMember("task") && pd["task"].IsInt() ? pd["task"].GetInt() : 0;
			scenario.createPed(pd["model"].GetString(), pd["relativeForward"].GetFloat(), pd["relativeRight"].GetFloat(), pd["relativeUp"].GetFloat(), pd["heading"].GetFloat(), pd["placeOnGround"].GetBool(), pd["animDict"].GetString(), pd["animName"].GetString(), ptask);
		}
		else if (d.HasMember("CreateVehicle")) {
			const Value& vd = d["CreateVehicle"];
			// [longtail] speed / drivingMode / actorId are optional so old scripts still work
			float sp = vd.HasMember("speed") ? vd["speed"].GetFloat() : 2.0f;
			int dm = vd.HasMember("drivingMode") ? vd["drivingMode"].GetInt() : 16777216;
			int aid = vd.HasMember("actorId") ? vd["actorId"].GetInt() : -1;
			scenario.createVehicle(vd["model"].GetString(), vd["relativeForward"].GetFloat(), vd["relativeRight"].GetFloat(), vd["heading"].GetFloat(), vd["color"].GetInt(), vd["color2"].GetInt(), vd["placeOnGround"].GetBool(), vd["withLifeJacketPed"].GetBool(), sp, dm, aid);
		}
		// ---------------- [longtail] long-tail orchestration ----------------
		else if (d.HasMember("SetSurvivalMode")) {
			const Value& sm = d["SetSurvivalMode"];
			scenario.setSurvivalMode(sm["policeIgnore"].GetBool(), sm["everyoneIgnore"].GetBool(),
				sm["playerInvincible"].GetBool(), sm["vehicleInvincible"].GetBool(),
				sm["seatbelt"].GetBool(), sm["aggressiveness"].GetFloat(), sm["ability"].GetFloat());
		}
		else if (d.HasMember("TaskVehicleTempAction")) {
			const Value& ta = d["TaskVehicleTempAction"];
			scenario.taskVehicleTempAction(ta["actorId"].GetInt(), ta["action"].GetInt(), ta["durationMs"].GetInt());
		}
		else if (d.HasMember("TaskVehicleDriveToCoord")) {
			const Value& tc = d["TaskVehicleDriveToCoord"];
			scenario.taskVehicleDriveToCoord(tc["actorId"].GetInt(), tc["x"].GetFloat(), tc["y"].GetFloat(), tc["z"].GetFloat(), tc["speed"].GetFloat(), tc["drivingMode"].GetInt());
		}
		else if (d.HasMember("SetEgoDrivingMode")) {
			const Value& ed = d["SetEgoDrivingMode"];
			scenario.setEgoDrivingMode(ed["drivingMode"].GetInt(), ed["setSpeed"].GetFloat());
		}
		else if (d.HasMember("ReplayControl")) {
			const Value& rc = d["ReplayControl"];
			scenario.replayControl(std::string(rc["action"].GetString()),
				rc.HasMember("a") ? rc["a"].GetFloat() : 0.0f,
				rc.HasMember("b") ? rc["b"].GetFloat() : 0.0f,
				rc.HasMember("frames") ? rc["frames"].GetInt() : 0);
		}
		else if (d.HasMember("SetCapturePause")) {
			scenario.setCapturePause(d["SetCapturePause"]["enabled"].GetBool());
		}
		else if (d.HasMember("SetClipRecording")) {
			const Value& cr = d["SetClipRecording"];
			scenario.setClipRecording(std::string(cr["action"].GetString()),
				cr.HasMember("mode") ? cr["mode"].GetInt() : 1,
				cr.HasMember("control") ? cr["control"].GetInt() : -1,
				cr.HasMember("group") ? cr["group"].GetInt() : 0,
				cr.HasMember("frames") ? cr["frames"].GetInt() : 3);
		}
		else if (d.HasMember("SetSceneDensity")) {
			const Value& sd = d["SetSceneDensity"];
			scenario.setSceneDensity(sd["vehicle"].GetFloat(), sd["randomVehicle"].GetFloat(),
				sd["parkedVehicle"].GetFloat(), sd["ped"].GetFloat(), sd["scenarioPed"].GetFloat());
		}
		else if (d.HasMember("SetCameraMount")) {
			const Value& cm = d["SetCameraMount"];
			scenario.setCameraMountFractions(
				cm.HasMember("forwardFrac") ? cm["forwardFrac"].GetFloat() : 0.35f,
				cm.HasMember("upFrac") ? cm["upFrac"].GetFloat() : 0.95f);
		}
		else if (d.HasMember("SeedScene")) {
			const Value& ss = d["SeedScene"];
			int nv = ss.HasMember("vehicles") ? ss["vehicles"].GetInt() : 0;
			int np = ss.HasMember("peds") ? ss["peds"].GetInt() : 0;
			int nc = ss.HasMember("cyclists") ? ss["cyclists"].GetInt() : 0;
			float rr = ss.HasMember("radius") ? ss["radius"].GetFloat() : 120.0f;
			unsigned sd = ss.HasMember("seed") ? (unsigned)ss["seed"].GetUint() : 0u;
			bool ca = ss.HasMember("clearAmbient") ? ss["clearAmbient"].GetBool() : false;
			scenario.seedScene(nv, np, nc, rr, sd, ca);
		}
		else if (d.HasMember("SetIgniteWrecks")) {
			const Value& iw = d["SetIgniteWrecks"];
			scenario.setIgniteWrecks(iw.HasMember("enabled") ? iw["enabled"].GetBool() : true,
				iw.HasMember("belowHealth") ? iw["belowHealth"].GetFloat() : 0.0f);
		}
		else if (d.HasMember("SetActorBehaviour")) {
			const Value& ab = d["SetActorBehaviour"];
			scenario.setActorBehaviour(
				ab.HasMember("drivingStyle") ? ab["drivingStyle"].GetInt() : (512 | 262144),
				ab.HasMember("aggressiveness") ? ab["aggressiveness"].GetFloat() : 1.0f,
				ab.HasMember("ability") ? ab["ability"].GetFloat() : 0.0f,
				ab.HasMember("cruiseSpeed") ? ab["cruiseSpeed"].GetFloat() : 40.0f,
				ab.HasMember("steersAround") ? ab["steersAround"].GetBool() : false,
				ab.HasMember("trafficAggression") ? ab["trafficAggression"].GetBool() : true,
				ab.HasMember("pedInteraction") ? ab["pedInteraction"].GetBool() : true,
				ab.HasMember("pedCrossChance") ? ab["pedCrossChance"].GetFloat() : 0.35f);
		}
		else if (d.HasMember("SetRoadLeash")) {
			const Value& rl = d["SetRoadLeash"];
			bool en = rl.HasMember("enabled") ? rl["enabled"].GetBool() : true;
			float dist = rl.HasMember("dist") ? rl["dist"].GetFloat() : 0.0f;
			float secs = rl.HasMember("seconds") ? rl["seconds"].GetFloat() : 0.0f;
			scenario.setRoadLeash(en, dist, secs);
		}
		else if (d.HasMember("SetTimeScale")) {
			const Value& ts = d["SetTimeScale"];
			scenario.setTimeScale(ts["scale"].GetFloat());
		}
		else if (d.HasMember("PrepareLocation")) {
			const Value& pl = d["PrepareLocation"];
			scenario.prepareLocation(pl["x"].GetFloat(), pl["y"].GetFloat(), pl["z"].GetFloat());
		}
		else if (d.HasMember("SetWeather")) {
			const Value& wd = d["SetWeather"];
			scenario.setWeather(wd["weather"].GetString());
		}
		else if (d.HasMember("SetClockTime")) {
			const Value& ct = d["SetClockTime"];
			scenario.setClockTime(ct["hour"].GetInt(), ct["minute"].GetInt(), ct["second"].GetInt());
		}

		else {
			return; //Invalid message
		}
	}

}






void Server::checkSendMessage() {
	logFrame("Server::CheckSendMessage");

		
	// TODO  Maybe some speed improvement could be made here by using the buffers more efficiently (zero copy)
		
	// Note that scenario.generateMessage() calls exporter.screenCapturer.capture() so this ordering is relevant
	StringBuffer messageJSON = scenario.generateMessage();
	string data = messageJSON.GetString();

	// Send Image TODO
	// [rockstar] Frames off: an empty frame message, and no capturer needed.
	ScreenCapturer* cap = scenario.exporter.screenCapturer;
	if (scenario.exporter.captureFrames() && cap != NULL) {
		socket.send(zmq::buffer(cap->pixels, cap->length));
	} else {
		zmq::message_t empty;
		socket.send(empty, zmq::send_flags::sndmore);
	}

	// Send JSON
	socket.send(zmq::buffer(data), zmq::send_flags::none);

	lastSentMessageTime = std::clock();

	
}