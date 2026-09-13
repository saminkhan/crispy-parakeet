// [longtail] Definitions that upstream live in ObjectDet/ObjectDetection.cpp.
//
// The longtail build captures RGB + camera pose only, so it excludes
// ObjectDet/ObjectDetection.cpp and ObjectDet/LiDAR.cpp -- which drops the
// OpenCV, Boost and GTAVisionNative dependencies entirely. But s_camParams is
// declared extern in CamParams.h and *defined* in ObjectDetection.cpp, and the
// camera-pose / intrinsics export needs it, so its definition moves here.
//
// NOTE: CamParams.h declares no includes of its own -- it assumes Vector3
// (lib/script.h) and Eigen are already visible at the point of inclusion.
#include "ObjectDetIncludes.h"
#include <Eigen/Core>
#include "CamParams.h"

CamParams s_camParams;
