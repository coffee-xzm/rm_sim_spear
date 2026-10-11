#pragma once

#include <deque>
#include <string>

#include "Components/SceneComponent.h"
#include "CoreMinimal.h"
#include "SpCore/SpFuncComponent.h"
#include "RmSimSpearReadbackMetadataComponent.generated.h"

// Companion component for a USpSceneCaptureComponent2D. It mirrors the
// capture component's enqueue/readback sequence and stores source identity in
// the SpFunc info field, so image bytes remain in SPEAR shared memory.
UCLASS(ClassGroup = "rm_sim_spear", meta = (BlueprintSpawnableComponent))
class RMSIMSPEARMETADATA_API URmSimSpearReadbackMetadataComponent : public USceneComponent
{
    GENERATED_BODY()

public:
    URmSimSpearReadbackMetadataComponent();

    void BeginPlay() override;
    void EndPlay(const EEndPlayReason::Type EndPlayReason) override;

private:
    struct QueuedMetadata
    {
        std::string info;
    };

    std::string addFrameId(const std::string& info) const;

    UPROPERTY(VisibleAnywhere, Category = "SPEAR")
    USpFuncComponent* SpFuncComponent = nullptr;

    std::deque<QueuedMetadata> queued_metadata_;
};
