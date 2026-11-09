# slopetanium-ui: the handheld's menu (ROCKNIXDS Pixel engine + banana-ui's software compositor, shown through SDL2).
#   make                      for this machine (needs SDL2 and FreeType headers)
#   make CROSS=aarch64-linux-gnu- OUT=slopetanium-ui.aarch64    a cross build (tools/build-ui.sh does it in Docker)
CROSS ?=
CXX := $(CROSS)g++
PKG_CONFIG ?= pkg-config
OUT ?= slopetanium-ui
CXXFLAGS ?= -O2
override CXXFLAGS += -std=c++17 -Wall -Wno-unused-function -Wno-unused-variable -ffunction-sections -fdata-sections
SRC = src/main.cpp src/net.cpp third_party/rnds/RndsRaster.cpp third_party/rnds/RndsUI.cpp \
      third_party/banana/soft.cpp third_party/banana/json.cpp
INC = $(shell $(PKG_CONFIG) --cflags sdl2 freetype2)
LIBS = $(shell $(PKG_CONFIG) --libs sdl2 freetype2) -lpthread
# libstdc++ and libgcc inside: the handhelds' own versions vary; SDL2 and FreeType come from the device
LDFLAGS += -static-libstdc++ -static-libgcc -Wl,--gc-sections -Wl,--as-needed

$(OUT): $(SRC) $(wildcard src/*.h third_party/*/*.h)
	$(CXX) $(CXXFLAGS) $(INC) -o $@ $(SRC) $(LDFLAGS) $(LIBS)
	$(CROSS)strip $@

clean:
	rm -f slopetanium-ui slopetanium-ui.*
.PHONY: clean
