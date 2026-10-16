#pragma once
// Reduced-motion support shared by every animation in the GUI.
namespace Motion {

// True when the user asked for less motion: TTS_REDUCE_MOTION=1, the "reduceMotion" app setting,
// or Windows "Show animations" turned off. Cached for a couple of seconds.
bool reduced();

// Animation duration in ms; 0 means "jump straight to the end state".
inline int ms(int base) { return reduced() ? 0 : base; }

}  // namespace Motion
