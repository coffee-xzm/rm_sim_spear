#include "RuneProjectile.h"

#include "Components/PointLightComponent.h"
#include "Components/SphereComponent.h"
#include "Components/StaticMeshComponent.h"
#include "GameFramework/ProjectileMovementComponent.h"
#include "Rune.h"
#include "RuneArmorTargetComponent.h"
#include "UObject/ConstructorHelpers.h"

ARuneProjectile::ARuneProjectile()
{
	PrimaryActorTick.bCanEverTick = false;
	CollisionComponent = CreateDefaultSubobject<USphereComponent>(TEXT("Collision"));
	CollisionComponent->InitSphereRadius(0.85f);
	CollisionComponent->SetCollisionProfileName(TEXT("RuneProjectile"));
	CollisionComponent->SetNotifyRigidBodyCollision(true);
	CollisionComponent->OnComponentHit.AddDynamic(this, &ARuneProjectile::OnHit);
	RootComponent = CollisionComponent;

	MovementComponent = CreateDefaultSubobject<UProjectileMovementComponent>(TEXT("Movement"));
	MovementComponent->InitialSpeed = InitialSpeed;
	MovementComponent->MaxSpeed = InitialSpeed;
	MovementComponent->bRotationFollowsVelocity = true;
	MovementComponent->bShouldBounce = false;
	MovementComponent->bSweepCollision = true;
	MovementComponent->bForceSubStepping = true;
	MovementComponent->MaxSimulationTimeStep = 1.0f / 120.0f;
	MovementComponent->ProjectileGravityScale = 1.0f;
	MovementComponent->SetUpdatedComponent(CollisionComponent);

	VisualMesh = CreateDefaultSubobject<UStaticMeshComponent>(TEXT("VisualMesh"));
	VisualMesh->SetupAttachment(RootComponent);
	VisualMesh->SetCollisionEnabled(ECollisionEnabled::NoCollision);
	VisualMesh->SetRelativeScale3D(FVector(0.017f));
	static ConstructorHelpers::FObjectFinder<UStaticMesh> SphereMesh(TEXT("/Engine/BasicShapes/Sphere.Sphere"));
	if (SphereMesh.Succeeded())
	{
		VisualMesh->SetStaticMesh(SphereMesh.Object);
	}
	GlowLight = CreateDefaultSubobject<UPointLightComponent>(TEXT("GlowLight"));
	GlowLight->SetupAttachment(RootComponent);
	GlowLight->SetLightColor(FLinearColor(0.05f, 1.0f, 0.15f));
	GlowLight->SetIntensity(250.0f);
	GlowLight->SetAttenuationRadius(40.0f);

	InitialLifeSpan = 5.0f;
}

void ARuneProjectile::OnHit(
	UPrimitiveComponent* HitComponent,
	AActor* OtherActor,
	UPrimitiveComponent* OtherComp,
	FVector NormalImpulse,
	const FHitResult& Hit)
{
	if (bProcessedHit)
	{
		return;
	}
	bProcessedHit = true;
	CollisionComponent->SetCollisionEnabled(ECollisionEnabled::NoCollision);

	if (URuneArmorTargetComponent* Target = Cast<URuneArmorTargetComponent>(OtherComp))
	{
		if (ARune* Rune = Target->GetRune())
		{
			Rune->HandleArmorHit(Target->GetArmorIndex(), Hit.ImpactPoint, ShooterTeam);
		}
	}

	SetLifeSpan(0.05f);
}
