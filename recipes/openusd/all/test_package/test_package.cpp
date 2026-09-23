#include <iostream>

#include <pxr/base/plug/plugin.h>
#include <pxr/base/plug/registry.h>
#include <pxr/usd/usd/stage.h>
#include <pxr/usd/usdGeom/mesh.h>
#include <pxr/usd/usdGeom/metrics.h>
#include <pxr/usd/usdGeom/xform.h>

PXR_NAMESPACE_USING_DIRECTIVE

int main() {
  UsdStageRefPtr stage = UsdStage::CreateNew("HelloWorld.usda");

  UsdGeomSetStageUpAxis(stage, UsdGeomTokens->y);
  UsdGeomSetStageMetersPerUnit(stage, 0.01);

  UsdGeomXform xform = UsdGeomXform::Define(stage, SdfPath("/root"));
  UsdGeomMesh mesh = UsdGeomMesh::Define(stage, SdfPath("/root/mesh"));
  stage->SetDefaultPrim(xform.GetPrim());

  stage->GetRootLayer()->Save();

  // hioOpenEXR is never linked by this executable: it's a runtime plugin
  // discovered through USD's Plug registry (plugInfo.json). Finding and
  // loading it here proves that discovery actually works from an installed
  // package, not just that the .so happens to be present on disk.
  PlugPluginPtr plugin = PlugRegistry::GetInstance().GetPluginWithName("hioOpenEXR");
  if (!plugin) {
    std::cerr << "Plug registry did not discover the hioOpenEXR plugin" << std::endl;
    return EXIT_FAILURE;
  }
  if (!plugin->Load()) {
    std::cerr << "Failed to load the hioOpenEXR plugin" << std::endl;
    return EXIT_FAILURE;
  }

  return EXIT_SUCCESS;
}
