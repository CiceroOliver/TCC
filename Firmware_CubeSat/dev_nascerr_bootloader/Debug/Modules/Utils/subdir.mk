################################################################################
# Automatically-generated file. Do not edit!
# Toolchain: GNU Tools for STM32 (14.3.rel1)
################################################################################

# Add inputs and outputs from these tool invocations to the build variables 
C_SRCS += \
../Modules/Utils/NASCERR_SysTime.c \
../Modules/Utils/NASCERR_Util.c 

OBJS += \
./Modules/Utils/NASCERR_SysTime.o \
./Modules/Utils/NASCERR_Util.o 

C_DEPS += \
./Modules/Utils/NASCERR_SysTime.d \
./Modules/Utils/NASCERR_Util.d 


# Each subdirectory must supply rules for building sources it contributes
Modules/Utils/%.o Modules/Utils/%.su Modules/Utils/%.cyclo: ../Modules/Utils/%.c Modules/Utils/subdir.mk
	arm-none-eabi-gcc "$<" -mcpu=cortex-m4 -std=gnu11 -g3 -DDEBUG -DUSE_HAL_DRIVER -DSTM32L4S5xx -c -I../Core/Inc -I../Modules/Communication -I../Modules/Database -I../Drivers/STM32L4xx_HAL_Driver/Inc -I../Drivers/STM32L4xx_HAL_Driver/Inc/Legacy -I../Drivers/CMSIS/Device/ST/STM32L4xx/Include -I../Drivers/CMSIS/Include -I../Modules/Bootloader -I../Modules/ExternalFlash -I../Modules/InternalFlash -I../Modules/System -I../Modules/Utils -I../Modules/Application -O0 -ffunction-sections -fdata-sections -Wall -fstack-usage -fcyclomatic-complexity -MMD -MP -MF"$(@:%.o=%.d)" -MT"$@" --specs=nano.specs -mfpu=fpv4-sp-d16 -mfloat-abi=hard -mthumb -o "$@"

clean: clean-Modules-2f-Utils

clean-Modules-2f-Utils:
	-$(RM) ./Modules/Utils/NASCERR_SysTime.cyclo ./Modules/Utils/NASCERR_SysTime.d ./Modules/Utils/NASCERR_SysTime.o ./Modules/Utils/NASCERR_SysTime.su ./Modules/Utils/NASCERR_Util.cyclo ./Modules/Utils/NASCERR_Util.d ./Modules/Utils/NASCERR_Util.o ./Modules/Utils/NASCERR_Util.su

.PHONY: clean-Modules-2f-Utils

