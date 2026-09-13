#include "ScreenCapturer.h"
#include "lib/script.h"
#include "lib/main.h"
#include "ObjectDet/Functions.h"
#include <stdlib.h>
#include <string.h>
#include <sstream>

#pragma comment(lib, "d3d11.lib")
#pragma comment(lib, "dxgi.lib")

// presentCallbackRegister takes a plain function pointer, so the live instance
// has to be reachable from file scope.
static ScreenCapturer *g_capturer = NULL;
// ⚠ ScreenCapturer is constructed PER SCENARIO (DataExport.cpp), but ScriptHookV
// treats a duplicate presentCallbackRegister as FATAL and terminates the game:
//     "FATAL: IDXGISwapChain::Present() callback is already registered"
// So register the thunk exactly once for the lifetime of the module and let it
// follow whichever instance is current.
static bool g_presentRegistered = false;

static void OnPresentThunk(void *chain) {
	if (g_capturer) g_capturer->onPresent(chain);
}

ScreenCapturer::ScreenCapturer(int Width, int Height) {
	windowWidth = Width;
	windowHeight = Height;

	// 24bpp rows padded to a 4-byte boundary: the layout VPilot's frame2numpy
	// already expects ("scanlines are aligned to 4 bytes in Windows bitmaps").
	length = ((windowWidth * 3 + 3) / 4 * 4) * windowHeight;
	pixels = (UINT8 *)malloc(length);
	if (pixels) memset(pixels, 0, length);

	shadow = (UINT8 *)malloc(length);
	if (shadow) memset(shadow, 0, length);

	staging = NULL;
	stagingW = stagingH = 0;
	enabled = haveFrame = 0;
	lastW = lastH = 0;

	g_capturer = this;
	if (!g_presentRegistered) {
		presentCallbackRegister(OnPresentThunk);
		g_presentRegistered = true;
		log("ScreenCapturer: registered IDXGISwapChain::Present callback");
	}
}

ScreenCapturer::~ScreenCapturer() {
	// Deliberately NOT unregistering: the thunk is shared across instances and a
	// later scenario will install a new one. OnPresentThunk null-checks g_capturer.
	if (g_capturer == this) g_capturer = NULL;
	releaseStaging();
	if (pixels) free(pixels);
	if (shadow) free(shadow);
}

void ScreenCapturer::releaseStaging() {
	if (staging) { staging->Release(); staging = NULL; }
	stagingW = stagingH = 0;
}

// Runs on the game's render thread. Only does work when the script side has
// asked for a frame, so the steady-state cost is one interlocked read.
void ScreenCapturer::onPresent(void *chain) {
	if (InterlockedCompareExchange(&enabled, 1, 1) != 1) return;
	// ⚠ Grab only when one has been asked for. Mapping a STAGING texture forces a
	// GPU->CPU sync; doing that on every Present (60+/s) stalls the pipeline hard
	// enough to risk a driver reset, and we only consume ~20-30 frames/s anyway.
	if (InterlockedCompareExchange(&request, 0, 1) != 1) return;
	grab((IDXGISwapChain *)chain);
	InterlockedExchange(&haveFrame, 1);
}

void ScreenCapturer::setEnabled(bool on) {
	InterlockedExchange(&enabled, on ? 1 : 0);
	if (on) {
		InterlockedExchange(&request, 1);      // prime the first frame
	} else {
		InterlockedExchange(&haveFrame, 0);
		InterlockedExchange(&request, 0);
	}
}

void ScreenCapturer::grab(IDXGISwapChain *chain) {
	if (!chain || !shadow) return;

	ID3D11Texture2D *back = NULL;
	if (FAILED(chain->GetBuffer(0, __uuidof(ID3D11Texture2D), (void **)&back)) || !back) return;

	ID3D11Device *dev = NULL;
	ID3D11DeviceContext *ctx = NULL;
	back->GetDevice(&dev);
	if (!dev) { back->Release(); return; }
	dev->GetImmediateContext(&ctx);
	if (!ctx) { dev->Release(); back->Release(); return; }
	// (GetDevice/GetImmediateContext AddRef; both are Released at the end.)

	D3D11_TEXTURE2D_DESC bd;
	back->GetDesc(&bd);

	// ⚠ The resample below indexes the source as 4 BYTES PER PIXEL. If the
	// backbuffer is not a 32-bit format that reads past the end of each row --
	// heap corruption, which shows up as a silent process death with no exception
	// rather than anything diagnosable. Log the real format once and refuse to
	// touch anything we cannot index safely.
	{
		static bool logged = false;
		if (!logged) {
			logged = true;
			std::ostringstream d;
			d << "[longtail] backbuffer " << bd.Width << "x" << bd.Height
			  << " fmt=" << (int)bd.Format
			  << " samples=" << bd.SampleDesc.Count
			  << " (target " << windowWidth << "x" << windowHeight << ")";
			log(d.str(), true);
		}
	}
	const bool fmt32 = (bd.Format == DXGI_FORMAT_R8G8B8A8_UNORM ||
	                    bd.Format == DXGI_FORMAT_R8G8B8A8_UNORM_SRGB ||
	                    bd.Format == DXGI_FORMAT_B8G8R8A8_UNORM ||
	                    bd.Format == DXGI_FORMAT_B8G8R8A8_UNORM_SRGB ||
	                    bd.Format == DXGI_FORMAT_R10G10B10A2_UNORM);
	if (!fmt32) {
		static bool warned = false;
		if (!warned) {
			warned = true;
			std::ostringstream d;
			d << "[longtail] UNSUPPORTED backbuffer format " << (int)bd.Format
			  << " -- refusing to sample (would read out of bounds)";
			log(d.str(), true);
		}
		// ⚠ dev and ctx were AddRef'd by GetDevice/GetImmediateContext above and
		// this early-out used to drop only `back`. That leaks one ID3D11Device and
		// one ID3D11DeviceContext reference PER PRESENT -- per rendered frame, not
		// per captured frame -- on any machine whose backbuffer is not a 32-bit
		// format (HDR output gives R16G16B16A16_FLOAT). Not reachable here, where
		// the backbuffer measures B8G8R8A8_UNORM, but it is exactly the shape of
		// fault that ends a long run with no exception to look at.
		ctx->Release(); dev->Release(); back->Release();
		return;
	}
	InterlockedExchange(&lastW, (LONG)bd.Width);
	InterlockedExchange(&lastH, (LONG)bd.Height);

	// (Re)create the CPU-readable staging texture when the backbuffer changes.
	if (!staging || stagingW != (int)bd.Width || stagingH != (int)bd.Height) {
		releaseStaging();
		D3D11_TEXTURE2D_DESC sd = bd;
		sd.Usage = D3D11_USAGE_STAGING;
		sd.BindFlags = 0;
		sd.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
		sd.MiscFlags = 0;
		sd.SampleDesc.Count = 1;      // staging cannot be multisampled
		sd.SampleDesc.Quality = 0;
		if (FAILED(dev->CreateTexture2D(&sd, NULL, &staging))) {
			ctx->Release(); dev->Release(); back->Release();
			return;
		}
		stagingW = (int)bd.Width;
		stagingH = (int)bd.Height;
	}

	if (bd.SampleDesc.Count > 1) {
		ctx->ResolveSubresource(staging, 0, back, 0, bd.Format);
	} else {
		ctx->CopyResource(staging, back);
	}

	D3D11_MAPPED_SUBRESOURCE map;
	if (SUCCEEDED(ctx->Map(staging, 0, D3D11_MAP_READ, 0, &map))) {
		const bool isRGBA = (bd.Format == DXGI_FORMAT_R8G8B8A8_UNORM ||
		                     bd.Format == DXGI_FORMAT_R8G8B8A8_UNORM_SRGB ||
		                     bd.Format == DXGI_FORMAT_R10G10B10A2_UNORM);
		const int dstStride = ((windowWidth * 3 + 3) / 4) * 4;
		const UINT8 *src = (const UINT8 *)map.pData;

		// The backbuffer is the window's client size, which need not equal the
		// requested frame size. Nearest-neighbour rather than silently emitting a
		// wrong-sized or cropped image.
		for (int y = 0; y < windowHeight; ++y) {
			const int sy = (stagingH == windowHeight)
			             ? y : (int)((INT64)y * stagingH / windowHeight);
			const UINT8 *srow = src + (size_t)sy * map.RowPitch;
			UINT8 *drow = shadow + (size_t)y * dstStride;
			for (int x = 0; x < windowWidth; ++x) {
				const int sx = (stagingW == windowWidth)
				             ? x : (int)((INT64)x * stagingW / windowWidth);
				const UINT8 *p = srow + (size_t)sx * 4;
				// destination is 24bpp BGR (what frame2numpy/cv2 expect)
				if (isRGBA) { drow[x*3+0] = p[2]; drow[x*3+1] = p[1]; drow[x*3+2] = p[0]; }
				else        { drow[x*3+0] = p[0]; drow[x*3+1] = p[1]; drow[x*3+2] = p[2]; }
			}
		}
		ctx->Unmap(staging, 0);
	}

	ctx->Release();
	dev->Release();
	back->Release();
}

// Runs on the script fiber, with the game paused. Non-blocking by design -- see
// the note in the header about why waiting here deadlocks.
void ScreenCapturer::capture() {
	if (InterlockedCompareExchange(&enabled, 1, 1) != 1) setEnabled(true);

	// Take whatever the render thread has most recently produced, then ask for the
	// next one. Still non-blocking -- waiting here deadlocks, because the game is
	// paused and Present cannot run until this returns.
	if (InterlockedCompareExchange(&haveFrame, 1, 1) == 1 && shadow && pixels) {
		memcpy(pixels, shadow, length);
	}
	InterlockedExchange(&request, 1);
}
