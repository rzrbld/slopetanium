// Retained modals must match flat composition, including updates beneath them.
#include "../ui/third_party/banana/soft.h"
#include <cassert>
#include <cstdio>
using banana::SoftBackend;
using rnds::Color;
static void scene(SoftBackend& b, int frame) {
    b.fill(0,0,200,100,Color::hex(0x243849));
    b.pushClip(8,4,70,40);
    b.fill(frame%3+5,6,72,36,Color::hex(0x904433,0.4f));
    b.popClip();
    b.fill(3,50,40,10,Color::hex(frame<4?0x42b83a:0x245bfa));
}
static void modal(SoftBackend& b, int frame) {
    if (frame==0 || frame==5 || frame==6) return;
    b.fill(0,0,200,100,Color::hex(0,0.5f));
    b.fill(18,12,48,38,Color::hex(0x758594));
    b.fill(22,frame==3?20:24,38,8,Color::hex(0xebce45,0.7f));
}
static void constantTextures() {
    for (bool smooth : {false,true}) for (unsigned alpha : {0u,1u,127u,254u,255u}) {
        SoftBackend fast(80,64), reference(80,64);
        rnds::Bitmap b; b.w=4; b.h=4; b.px.resize(64);
        for(int i=0;i<16;i++) {b.px[i*4]=83;b.px[i*4+1]=149;b.px[i*4+2]=221;b.px[i*4+3]=alpha;}
        auto a=fast.upload(b,smooth), old=reference.upload(b,smooth);
        std::static_pointer_cast<banana::SoftTex>(old)->uniform=false;
        for (float opacity : {0.0f,0.37f,1.0f}) {
            fast.beginScreen(1); reference.beginScreen(1);
            for(auto* dst : {&fast,&reference}) {dst->fill(0,0,80,64,Color::hex(0x324678));dst->pushClip(3,2,71,57);}
            Color tint(0.27f,0.61f,0.93f,opacity);
            fast.draw(a,1.25f,0.75f,75.5f,48.25f,tint);
            reference.draw(old,1.25f,0.75f,75.5f,48.25f,tint);
            fast.popClip(); reference.popClip();fast.endScreen();reference.endScreen();
            for(int i=0;i<80*64;i++) assert(fast.pixels(1)[i]==reference.pixels(1)[i]);
        }
    }
}
int main() {
    constantTextures();
    SoftBackend layered(80,64,112), flat(80,64,112);
    int frame=0;
    layered.overlay=[&](int){modal(layered,frame);};
    for (;frame<10;frame++) for(int screen=0;screen<2;screen++) {
        if(frame==7) {layered.invalidate();flat.invalidate();}
        layered.beginScreen(screen); scene(layered,frame); layered.endScreen();
        flat.beginScreen(screen); scene(flat,frame); modal(flat,frame); flat.endScreen();
        int n=flat.width(screen)*flat.height();
        for(int i=0;i<n;i++) assert(layered.pixels(screen)[i]==flat.pixels(screen)[i]);
        layered.takeDamage(screen); flat.takeDamage(screen);
    }
    layered.beginScreen(1); scene(layered,frame-1); layered.endScreen();
    assert(layered.takeDamage(1).empty());
    std::puts("overlay pixels match flat composition across transitions and unequal panels");
}
