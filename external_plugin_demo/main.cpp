#include <iostream>

#include <pxr/base/plug/plugin.h>
#include <pxr/base/plug/registry.h>
#include <pxr/base/tf/type.h>

PXR_NAMESPACE_USING_DIRECTIVE

int main() {
  PlugPluginPtr plugin = PlugRegistry::GetInstance().GetPluginWithName("myExternalPlugin");
  if (!plugin) {
    std::cerr << "NOT FOUND: PXR_PLUGINPATH_NAME did not pick up myExternalPlugin" << std::endl;
    return EXIT_FAILURE;
  }
  if (!plugin->Load()) {
    std::cerr << "FOUND but FAILED TO LOAD myExternalPlugin" << std::endl;
    return EXIT_FAILURE;
  }
  TfType t = TfType::FindByName("MyExternalPlugin");
  if (t.IsUnknown()) {
    std::cerr << "Plugin loaded but its TfType registration never ran" << std::endl;
    return EXIT_FAILURE;
  }
  std::cout << "OK: external plugin discovered, loaded, and registered" << std::endl;
  return EXIT_SUCCESS;
}
