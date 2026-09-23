// This is the "third-party" plugin: something nobody in the openusd recipe
// or its test_package ever compiles. It only proves one thing: that USD's
// Plug registry can find and load a plugin that lives completely outside
// the Conan package, via PXR_PLUGINPATH_NAME. See README.md.
#include <pxr/base/tf/type.h>

PXR_NAMESPACE_USING_DIRECTIVE

class MyExternalPlugin {};

TF_REGISTRY_FUNCTION(TfType) {
    TfType::Define<MyExternalPlugin>();
}
