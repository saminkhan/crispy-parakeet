#pragma once

#define WIN32_LEAN_AND_MEAN
#include <Windows.h>
#include <d3d11.h>
#include <dxgi.h>

// [longtail] Upstream captured frames by BitBlt-ing the DESKTOP (GetDC(NULL)) at
// (0,0). That is a screen scrape, not a capture of the game's render output, and
// on this setup it returns an all-zero buffer -- i.e. pure black frames alongside
// perfectly valid poses, metadata and quality gates. It also makes the capture
// hostage to window position, z-order, focus and anything that overlaps the
// top-left of the desktop.
//
// ScriptHookV exposes presentCallbackRegister(), which hands us the
// IDXGISwapChain on every Present. We copy the backbuffer into a STAGING texture
// and read it directly, which is what the game actually rendered -- independent
// of where the window is or what is on top of it.
class ScreenCapturer {
private:
	int windowWidth;
	int windowHeight;

	// D3D backbuffer path
	ID3D11Texture2D *staging;
	int stagingW, stagingH;
	// ⚠ capture() runs with the game PAUSED (DataExport.cpp: pause at 312,
	// capture at 378, unpause at 447). A request/wait handshake deadlocks there:
	// the script fiber waits for Present, but Present cannot happen until the
	// script returns and the game unpauses. So instead the render thread grabs
	// continuously while enabled, and capture() is a non-blocking copy of the
	// most recent frame. With the game paused, the last presented frame IS the
	// current scene, so frame and pose still agree.
	UINT8 *shadow;              // written by the render thread
	volatile LONG enabled;      // grabbing armed at all
	volatile LONG request;      // one-shot: grab on the next Present
	volatile LONG haveFrame;
	volatile LONG lastW, lastH; // last backbuffer size seen, for diagnostics

	void grab(IDXGISwapChain *chain);
	void releaseStaging();

public:
	int length;
	UINT8 *pixels;

	ScreenCapturer(int frameWidth, int frameHeight);
	~ScreenCapturer();
	void capture();
	void setEnabled(bool on);
	void onPresent(void *chain);      // called from the render thread
	int width()  const { return windowWidth; }
	int height() const { return windowHeight; }
	int backbufferWidth() const { return lastW; }
	int backbufferHeight() const { return lastH; }
};
